"""
Extracts emails and person/designation info from HTML.
Fully config-driven — no regexes or keyword lists hardcoded here.
Phone extraction is intentionally excluded.
"""

import re
import logging
from dataclasses import dataclass

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


@dataclass
class Lead:
    email: str | None = None
    person_name: str | None = None
    designation: str | None = None
    department: str | None = None
    source_url: str = ""
    context_snippet: str = ""


def parse_page(html: str, source_url: str, config: dict) -> list[Lead]:
    """
    Returns Lead objects extracted from the page.
    config = extraction section of portal/config.yaml
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        raw_text = soup.get_text(separator=" ")
    except Exception as e:
        log.debug(f"parse_page HTML parse error for {source_url}: {e}")
        return []

    leads: list[Lead] = []

    # Pass 1: table rows (highest confidence — structured data)
    if config.get("person", {}).get("enabled"):
        leads.extend(_extract_from_tables(soup, source_url, config))

    # Pass 2: proximity text scan — email as anchor, look for name/designation nearby
    text = _normalise_obfuscation(raw_text, config.get("email", {}))
    email_spans = _find_email_spans(text, config.get("email", {}))

    seen_emails = {l.email for l in leads if l.email}
    ctx_chars  = config.get("email", {}).get("context_chars", 200)
    prox_chars = config.get("person", {}).get("proximity_chars", 300)

    for email, start, end in email_spans:
        if email in seen_emails:
            continue
        seen_emails.add(email)

        context = " ".join(text[max(0, start - ctx_chars): end + ctx_chars].split())
        window  = text[max(0, start - prox_chars): end + prox_chars]
        name, desig = _extract_person_from_text(window, config.get("person", {}))

        leads.append(Lead(
            email=email,
            person_name=name,
            designation=desig,
            source_url=source_url,
            context_snippet=context,
        ))

    return leads


# ── Email extraction ──────────────────────────────────────────────────────────

def _normalise_obfuscation(text: str, ecfg: dict) -> str:
    for pattern, replacement in ecfg.get("obfuscation", []):
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _find_email_spans(text: str, ecfg: dict) -> list[tuple[str, int, int]]:
    if not ecfg.get("enabled", True):
        return []
    regex = ecfg.get("regex",
                     r"[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}")
    valid_suffixes = tuple(ecfg.get("valid_suffixes", [".gov.in", ".nic.in"]))
    seen: set[str] = set()
    results = []
    for m in re.finditer(regex, text, re.IGNORECASE):
        email = m.group(0).lower().strip(".")
        if not email.endswith(valid_suffixes) or email in seen:
            continue
        seen.add(email)
        results.append((email, m.start(), m.end()))
    return results


# ── Person / designation extraction ──────────────────────────────────────────

def _extract_person_from_text(window: str,
                               pcfg: dict) -> tuple[str | None, str | None]:
    prefixes       = pcfg.get("title_prefixes", [])
    desig_keywords = pcfg.get("designation_keywords", [])
    name, desig    = None, None

    if prefixes:
        pat = (r"\b(" + "|".join(re.escape(p) for p in prefixes) +
               r")\b\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})")
        m = re.search(pat, window)
        if m:
            name = f"{m.group(1)} {m.group(2)}".strip()

    if desig_keywords:
        pat = (r"\b(" + "|".join(re.escape(k) for k in desig_keywords) + r")\b")
        m = re.search(pat, window, re.IGNORECASE)
        if m:
            s = m.start()
            desig = " ".join(window[s: s + 60].split())[:60]

    return name, desig


# ── Table-based extraction (highest confidence) ───────────────────────────────

def _extract_from_tables(soup: BeautifulSoup, source_url: str,
                          config: dict) -> list[Lead]:
    ecfg  = config.get("email", {})
    pcfg  = config.get("person", {})
    leads: list[Lead] = []

    valid_suffixes = tuple(ecfg.get("valid_suffixes", [".gov.in", ".nic.in"]))
    email_re = re.compile(
        ecfg.get("regex",
                 r"[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}"),
        re.IGNORECASE,
    )

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [c.get_text(strip=True).lower()
                   for c in rows[0].find_all(["th", "td"])]

        col = {
            "name":        _find_col(headers, ["name", "officer", "official", "contact person"]),
            "designation": _find_col(headers, ["designation", "post", "rank", "position"]),
            "department":  _find_col(headers, ["department", "division", "ministry", "section"]),
            "email":       _find_col(headers, ["email", "e-mail", "mail"]),
        }

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            cell_texts = [c.get_text(separator=" ", strip=True) for c in cells]
            row_text   = " ".join(cell_texts)

            for m in email_re.finditer(row_text):
                email = m.group(0).lower().strip(".")
                if not email.endswith(valid_suffixes):
                    continue

                name  = _cell_value(cell_texts, col["name"])
                desig = _cell_value(cell_texts, col["designation"])
                dept  = _cell_value(cell_texts, col["department"])

                if not name and pcfg.get("title_prefixes"):
                    pm = re.search(
                        r"\b(" + "|".join(re.escape(p)
                                          for p in pcfg["title_prefixes"]) +
                        r")\b\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
                        row_text,
                    )
                    if pm:
                        name = f"{pm.group(1)} {pm.group(2)}".strip()

                if not desig and pcfg.get("designation_keywords"):
                    dm = re.search(
                        r"\b(" + "|".join(re.escape(k)
                                          for k in pcfg["designation_keywords"]) + r")\b",
                        row_text, re.IGNORECASE,
                    )
                    if dm:
                        s = dm.start()
                        desig = " ".join(row_text[s: s + 60].split())[:60]

                leads.append(Lead(
                    email=email,
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
