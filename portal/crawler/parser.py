"""
Extracts emails, phone numbers, and person/designation info from HTML.
Fully config-driven — no regexes or keyword lists hardcoded here.
"""

import re
import logging
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


@dataclass
class Lead:
    email: str | None = None
    phone: str | None = None
    person_name: str | None = None
    designation: str | None = None
    department: str | None = None
    source_url: str = ""
    context_snippet: str = ""


def parse_page(html: str, source_url: str, config: dict) -> list[Lead]:
    """
    Main entry point. Returns list of Lead objects extracted from the page.
    config = extraction section of config.yaml
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        raw_text = soup.get_text(separator=" ")
    except Exception as e:
        log.debug(f"parse_page failed to parse HTML for {source_url}: {e}")
        return []

    leads: list[Lead] = []

    # Extract table-based leads first (highest confidence)
    if config.get("person", {}).get("enabled"):
        leads.extend(_extract_from_tables(soup, source_url, config))

    # Then text-based extraction for anything tables missed
    text = _normalise_obfuscation(raw_text, config.get("email", {}))
    email_spans = _find_email_spans(text, config.get("email", {}))
    phone_spans = _find_phone_spans(text, config.get("phone", {}))

    # Build leads from email matches (primary anchor)
    seen_emails = {l.email for l in leads if l.email}
    ctx_chars = config.get("email", {}).get("context_chars", 200)
    prox_chars = config.get("person", {}).get("proximity_chars", 300)

    for email, start, end in email_spans:
        if email in seen_emails:
            continue
        seen_emails.add(email)

        context = " ".join(text[max(0, start - ctx_chars): end + ctx_chars].split())
        window = text[max(0, start - prox_chars): end + prox_chars]

        phone = _nearest_phone(phone_spans, start, end)
        name, desig = _extract_person_from_text(window, config.get("person", {}))

        leads.append(Lead(
            email=email,
            phone=phone,
            person_name=name,
            designation=desig,
            source_url=source_url,
            context_snippet=context,
        ))

    # Add phone-only leads (phones without associated emails, rare but valid)
    seen_phones = {l.phone for l in leads if l.phone}
    for phone, _, _ in phone_spans:
        if phone not in seen_phones and _looks_like_real_phone(phone):
            seen_phones.add(phone)
            leads.append(Lead(phone=phone, source_url=source_url))

    return leads


# ── Email extraction ──────────────────────────────────────────────────────────

def _normalise_obfuscation(text: str, ecfg: dict) -> str:
    for pattern, replacement in ecfg.get("obfuscation", []):
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _find_email_spans(text: str, ecfg: dict) -> list[tuple[str, int, int]]:
    if not ecfg.get("enabled", True):
        return []
    regex = ecfg.get("regex", r"[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}")
    valid_suffixes = tuple(ecfg.get("valid_suffixes", [".gov.in", ".nic.in"]))
    results = []
    seen = set()
    for m in re.finditer(regex, text, re.IGNORECASE):
        email = m.group(0).lower().strip(".")
        if not email.endswith(valid_suffixes):
            continue
        if email in seen:
            continue
        seen.add(email)
        results.append((email, m.start(), m.end()))
    return results


# ── Phone extraction ──────────────────────────────────────────────────────────

def _find_phone_spans(text: str, pcfg: dict) -> list[tuple[str, int, int]]:
    if not pcfg.get("enabled", True):
        return []
    results = []
    seen = set()
    for pattern in pcfg.get("patterns", []):
        for m in re.finditer(pattern, text):
            phone = re.sub(r"[\s\-]", "", m.group(0))
            if phone in seen:
                continue
            seen.add(phone)
            results.append((phone, m.start(), m.end()))
    return results


def _nearest_phone(phone_spans: list, email_start: int, email_end: int,
                   max_distance: int = 400) -> str | None:
    best_phone, best_dist = None, max_distance
    for phone, ps, pe in phone_spans:
        dist = min(abs(email_start - pe), abs(ps - email_end))
        if dist < best_dist:
            best_phone, best_dist = phone, dist
    return best_phone


def _looks_like_real_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return 8 <= len(digits) <= 13


# ── Person / designation extraction ──────────────────────────────────────────

def _extract_person_from_text(window: str, pcfg: dict) -> tuple[str | None, str | None]:
    prefixes = pcfg.get("title_prefixes", [])
    desig_keywords = pcfg.get("designation_keywords", [])

    name = None
    designation = None

    # Look for "Shri/Smt/Dr ... Name" pattern
    if prefixes:
        prefix_pattern = r"\b(" + "|".join(re.escape(p) for p in prefixes) + r")\b\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})"
        m = re.search(prefix_pattern, window)
        if m:
            name = f"{m.group(1)} {m.group(2)}".strip()

    # Look for designation keywords near the name/email
    if desig_keywords:
        desig_pattern = r"\b(" + "|".join(re.escape(k) for k in desig_keywords) + r")\b"
        m = re.search(desig_pattern, window, re.IGNORECASE)
        if m:
            # Take up to 60 chars starting at the match as the designation phrase
            start = m.start()
            designation = " ".join(window[start:start + 60].split())[:60]

    return name, designation


# ── Table-based extraction (highest confidence) ───────────────────────────────

def _extract_from_tables(soup: BeautifulSoup, source_url: str, config: dict) -> list[Lead]:
    """
    Finds HTML tables whose rows contain gov.in email addresses.
    Maps column headers to data cells to extract name/designation alongside the email.
    """
    ecfg = config.get("email", {})
    pcfg = config.get("person", {})
    leads: list[Lead] = []
    valid_suffixes = tuple(ecfg.get("valid_suffixes", [".gov.in", ".nic.in"]))
    email_regex = re.compile(
        ecfg.get("regex", r"[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}"),
        re.IGNORECASE
    )
    desig_keywords = [k.lower() for k in pcfg.get("designation_keywords", [])]
    prefixes = pcfg.get("title_prefixes", [])
    prefix_set = {p.lower() for p in prefixes}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Build column index from header row
        headers = []
        header_row = rows[0]
        for cell in header_row.find_all(["th", "td"]):
            headers.append(cell.get_text(strip=True).lower())

        # Map header keywords to column indices
        col = {
            "name":        _find_col(headers, ["name", "officer", "official", "contact person"]),
            "designation": _find_col(headers, ["designation", "post", "rank", "position"]),
            "department":  _find_col(headers, ["department", "division", "ministry", "section"]),
            "phone":       _find_col(headers, ["phone", "telephone", "tel", "mobile", "contact"]),
            "email":       _find_col(headers, ["email", "e-mail", "mail"]),
        }

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            cell_texts = [c.get_text(separator=" ", strip=True) for c in cells]
            row_text = " ".join(cell_texts)

            # Find emails anywhere in this row
            for m in email_regex.finditer(row_text):
                email = m.group(0).lower().strip(".")
                if not email.endswith(valid_suffixes):
                    continue

                name = _cell_value(cell_texts, col["name"])
                desig = _cell_value(cell_texts, col["designation"])
                dept = _cell_value(cell_texts, col["department"])
                phone_raw = _cell_value(cell_texts, col["phone"])
                phone = re.sub(r"[\s\-]", "", phone_raw) if phone_raw else None

                # If no header-mapped name, try prefix heuristic on row text
                if not name and prefixes:
                    pm = re.search(
                        r"\b(" + "|".join(re.escape(p) for p in prefixes) + r")\b\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
                        row_text
                    )
                    if pm:
                        name = f"{pm.group(1)} {pm.group(2)}".strip()

                # If no header-mapped designation, try keyword match
                if not desig:
                    dm = re.search(
                        r"\b(" + "|".join(re.escape(k) for k in pcfg.get("designation_keywords", [])) + r")\b",
                        row_text, re.IGNORECASE
                    )
                    if dm:
                        s = dm.start()
                        desig = " ".join(row_text[s:s + 60].split())[:60]

                leads.append(Lead(
                    email=email,
                    phone=phone if phone and _looks_like_real_phone(phone) else None,
                    person_name=name or None,
                    designation=desig or None,
                    department=dept or None,
                    source_url=source_url,
                    context_snippet=row_text[:300],
                ))

    return leads


def _find_col(headers: list[str], keywords: list[str]) -> int | None:
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i
    return None


def _cell_value(cells: list[str], col_idx: int | None) -> str | None:
    if col_idx is None or col_idx >= len(cells):
        return None
    v = cells[col_idx].strip()
    return v if v else None
