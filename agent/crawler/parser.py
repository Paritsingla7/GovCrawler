"""
Extracts emails and person/designation info from HTML.
Fully config-driven — no regexes or keyword lists hardcoded here.

6-stage pipes-and-filters pipeline:
  1. extract_candidates  — harvest (address, rung, context_node) tuples by signal ladder
  2. bind_channels       — group candidates into entities; resolve role/external tags
  3. enrich_fields       — name/designation/dept (gated by person.enabled); page-vs-domain flag
  4. normalise_spans     — guarded local de-obfuscation on bracketed forms only
  5. score               — assign confidence band (HIGH/LOW) from rung; build field_provenance JSON
  6. flatten_emit        — one flat Lead per email; band never drops a lead; only email-less entities skipped
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Rung order (higher index = higher precedence)
_RUNG_ORDER = ["proximity_text", "table_block", "microdata", "mailto_tel"]
_RUNG_RANK = {r: i for i, r in enumerate(_RUNG_ORDER)}

# Hard bounds on table-derived text — a degenerate/layout table (a single
# cell dumping an entire document) must never turn into an unbounded regex
# scan or an unbounded field value.
_MAX_CELL_CHARS = 500
_MAX_ROW_SCAN_CHARS = 5000

# mailto:/microdata email cleanup
_MAX_RECIPIENTS_PER_HREF = 5
_EMAIL_STRIP_CHARS = " \t\r\n,;<>'\""

# A keyword hit only tells us WHERE a designation starts, never how long it
# is — stop at the first sign the raw text ran past the title into embedded
# contact info (email, a phone label, a long digit run, or a line break).
_DESIGNATION_STOP_RE = re.compile(
    r"[@\n]|\b(?:e-?mail|phone|tel(?:ephone)?)\b\s*[:\-]?|\d{5,}",
    re.IGNORECASE,
)


@dataclass
class Lead:
    email: str | None = None
    person_name: str | None = None
    designation: str | None = None
    department: str | None = None
    source_url: str = ""
    source_title: str = ""
    context_snippet: str = ""
    entity_kind: str | None = None
    phone: str | None = None
    channel_tag: str | None = None
    confidence_band: str | None = None
    field_provenance: str | None = None


def parse_page(html: str, source_url: str, config: dict) -> list[Lead]:
    """Thin wrapper: builds the soup then delegates to extract_leads."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        log.debug(f"parse_page HTML parse error for {source_url}: {e}")
        return []
    return extract_leads(soup, source_url, config)


def extract_leads(soup: BeautifulSoup, source_url: str, config: dict) -> list[Lead]:
    """
    Returns Lead objects from an already-parsed BeautifulSoup tree.

    NOTE: this mutates `soup` (strips <script>/<style>/<noscript> before
    proximity-text stage). Structured signals (mailto hrefs, microdata, tables)
    are harvested from the full DOM before the decompose call.
    """
    try:
        ecfg = config.get("email", {})
        pcfg = config.get("person", {})
        conf_cfg = config.get("confidence", {})
        role_local_parts = set(
            config.get(
                "role_local_parts", ["webmaster", "info", "admin", "contact", "support", "helpdesk", "grievance"]
            )
        )
        max_input_chars = config.get("max_input_chars", 0)  # 0 = no cap
        high_rungs = set(conf_cfg.get("high_rungs", ["mailto_tel", "microdata"]))
        mid_rungs = set(conf_cfg.get("mid_rungs", ["table_block", "proximity_text"]))

        page_title = ""
        if soup.title and soup.title.string:
            page_title = soup.title.string.strip()

        # Stage 1: extract_candidates (before decompose — all DOM signals available)
        candidates = _extract_candidates(soup, ecfg, max_input_chars)

        # Decompose noise tags now (proximity text stage will use get_text)
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        # Stage 2: bind_channels
        entities = _bind_channels(candidates, role_local_parts, ecfg)

        # Stage 3: enrich_fields
        raw_text = soup.get_text(separator=" ")
        entities = _enrich_fields(entities, soup, raw_text, source_url, pcfg, ecfg)

        # Stage 4: normalise_spans (guarded de-obfuscation, proximity-text candidates only)
        entities = _normalise_spans(entities, ecfg, role_local_parts)

        # Stage 5: score
        entities = _score(entities, high_rungs, mid_rungs)

        # Stage 6: flatten_emit
        leads = _flatten_emit(entities, source_url, page_title)

    except Exception as e:
        log.warning(f"extract_leads pipeline error for {source_url}: {e}")
        return []

    return leads


def parse_for_engine(html: str, url: str, excfg: dict) -> tuple[list[Lead], list]:
    """
    Thread-pool target for CrawlerEngine. Builds ONE soup and returns:
      (leads, raw_links)
    raw_links is a list of (absolute_url, anchor_text, rel) — filtering happens
    on the event-loop thread (cheap string ops with access to the visited set).
    `rel` is the anchor's rel attribute normalised to a lowercased list of
    tokens (e.g. ["next"]), or [] when absent — used for pagination detection.

    Anchors are harvested BEFORE extract_leads runs, because extract_leads
    decomposes <script>/<style>/<noscript> from the tree.

    Uses html.parser (pure Python) instead of lxml to avoid C-extension
    thread-safety edge cases when many threads parse concurrently.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        log.warning(f"parse_for_engine: soup parse failed for {url}: {e}")
        return [], []

    raw_links: list[tuple[str, str, list[str]]] = []
    try:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            text = (a.get_text() or "").strip().lower()[:100]
            rel = [r.lower() for r in (a.get("rel") or [])]
            try:
                absolute = urljoin(url, href)
            except Exception:
                continue
            raw_links.append((absolute, text, rel))
    except Exception as e:
        log.warning(f"parse_for_engine: link extraction failed for {url}: {e}")
        raw_links = []

    try:
        leads = extract_leads(soup, url, excfg)
    except Exception as e:
        log.warning(f"parse_for_engine: extract_leads raised for {url}: {e}")
        leads = []

    if leads:
        log.debug(f"parse_for_engine: {len(leads)} leads at {url}")

    return leads, raw_links


# ── Stage 1: extract_candidates ──────────────────────────────────────────────


def _extract_candidates(soup: BeautifulSoup, ecfg: dict, max_input_chars: int) -> list[dict]:
    """
    Returns a list of candidate dicts:
      {address, rung, context_node, raw_span, phone}
    context_node is the bs4 Tag nearest to the candidate (for proximity grouping).
    """
    if not ecfg.get("enabled", True):
        return []

    email_re = re.compile(
        ecfg.get("regex", r"[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}"),
        re.IGNORECASE,
    )
    tel_re = re.compile(r"tel:(\+?[\d\s\-().]{7,20})", re.IGNORECASE)
    valid_suffixes = tuple(ecfg.get("valid_suffixes", [".gov.in", ".nic.in"]))
    candidates: list[dict] = []

    # 1a. mailto: and tel: hrefs (highest rung)
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if href.startswith("mailto:"):
            raw_addr = href[7:].split("?")[0]
            for addr in _clean_email_candidates(raw_addr, email_re):
                candidates.append(
                    {
                        "address": addr,
                        "rung": "mailto_tel",
                        "context_node": a,
                        "raw_span": addr,
                        "phone": None,
                    }
                )
        elif href.startswith("tel:"):
            m = tel_re.match(href)
            if m:
                phone_num = m.group(1).strip()
                candidates.append(
                    {
                        "address": None,
                        "rung": "mailto_tel",
                        "context_node": a,
                        "raw_span": phone_num,
                        "phone": phone_num,
                    }
                )

    # 1b. Microdata itemprop email/telephone
    for el in soup.find_all(itemprop=True):
        prop = el.get("itemprop", "").lower()
        if prop == "email":
            content = el.get("content") or el.get_text(strip=True)
            for addr in _clean_email_candidates(content, email_re):
                candidates.append(
                    {
                        "address": addr,
                        "rung": "microdata",
                        "context_node": el,
                        "raw_span": addr,
                        "phone": None,
                    }
                )
        elif prop in ("telephone", "phone"):
            content = el.get("content") or el.get_text(strip=True)
            if content:
                candidates.append(
                    {
                        "address": None,
                        "rung": "microdata",
                        "context_node": el,
                        "raw_span": content.strip(),
                        "phone": content.strip(),
                    }
                )

    # 1c. Table/card block scan
    candidates.extend(_extract_table_candidates(soup, email_re, valid_suffixes))

    # 1d. Proximity text scan (bounded by max_input_chars)
    # Apply obfuscation first so "webmaster at nic dot in" becomes "webmaster@nic.in"
    # before the email regex scans — same as the old parser did globally.
    page_text = soup.get_text(separator=" ")
    raw_text = page_text[:max_input_chars] if max_input_chars else page_text
    normalised_text = _apply_obfuscation(raw_text, ecfg)
    candidates.extend(_extract_proximity_candidates(normalised_text, email_re, ecfg))

    return candidates


def _clean_email_candidates(raw: str, email_re: re.Pattern) -> list[str]:
    """Turns one raw mailto-href/microdata-content string into zero or more
    clean, individually-valid email addresses. Handles URL-encoded artifacts
    (%20 etc.), multiple comma-joined recipients (RFC 6068 allows a mailto:
    href to list several), and stray punctuation a browser would never emit
    but a hand-written page sometimes does. Validates each candidate with a
    full-string match — a remainder that's still broken after cleanup (a
    doubled domain, an embedded space, an empty local part) is dropped
    rather than stored as a broken "email"."""
    if not raw:
        return []
    decoded = unquote(raw)
    pieces = decoded.split(",")[:_MAX_RECIPIENTS_PER_HREF]
    cleaned: list[str] = []
    for piece in pieces:
        candidate = piece.strip(_EMAIL_STRIP_CHARS).replace(" ", "").lower().strip(".")
        if candidate and email_re.fullmatch(candidate):
            cleaned.append(candidate)
    return cleaned


def _dedupe_col_map(col: dict) -> dict:
    """A layout/degenerate table can header-match more than one field role
    to the same cell index (e.g. one generic header column matching several
    keyword lists). Keep the highest-priority role's claim on that index and
    null the rest, so name/designation/department never silently copy the
    same cell verbatim."""
    seen: set[int] = set()
    deduped = {}
    for key in ("name", "designation", "department", "email"):
        idx = col.get(key)
        if idx is not None and idx in seen:
            deduped[key] = None
        else:
            deduped[key] = idx
            if idx is not None:
                seen.add(idx)
    return deduped


def _truncate_at_known_suffix(address: str, valid_suffixes: tuple) -> str:
    """Free-text scanning has no way to know where a real domain ends —
    `alltimeplastics.com` immediately followed by unrelated glued text with
    no separator (`ajay.sawale`) is syntactically just as valid a 3-label
    domain to the regex as the real 2-label one. If a configured
    valid_suffix appears anywhere inside the matched domain, truncate right
    after its first (leftmost) occurrence and drop everything past it —
    recovers "info@alltimeplastics.com" from
    "info@alltimeplastics.comajay.sawale". Leaves the address untouched
    when no configured suffix appears anywhere in it — there's no positive
    evidence of where to cut, so nothing is guessed or dropped."""
    local, _, domain = address.rpartition("@")
    if not domain:
        return address
    earliest_end = None
    for suffix in valid_suffixes:
        pos = domain.find(suffix)
        if pos != -1:
            end = pos + len(suffix)
            if earliest_end is None or end < earliest_end:
                earliest_end = end
    if earliest_end is None or earliest_end == len(domain):
        return address
    return f"{local}@{domain[:earliest_end]}"


def _extract_table_candidates(soup: BeautifulSoup, email_re: re.Pattern, valid_suffixes: tuple) -> list[dict]:
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        col = _dedupe_col_map(
            {
                "name": _find_col(headers, ["name", "officer", "official", "contact person"]),
                "designation": _find_col(headers, ["designation", "post", "rank", "position"]),
                "department": _find_col(headers, ["department", "division", "ministry", "section"]),
                "email": _find_col(headers, ["email", "e-mail", "mail"]),
            }
        )
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            cell_texts = [c.get_text(separator=" ", strip=True) for c in cells]
            row_text = " ".join(cell_texts)
            if len(row_text) > _MAX_ROW_SCAN_CHARS:
                # Degenerate row (e.g. a single cell dumping an entire
                # document's text) — skip rather than regex-scan an
                # unbounded blob and risk binding a CPU-hazard-sized span
                # to person_name/designation/department.
                continue
            for m in email_re.finditer(row_text):
                addr = _truncate_at_known_suffix(m.group(0).lower().strip("."), valid_suffixes)
                results.append(
                    {
                        "address": addr,
                        "rung": "table_block",
                        "context_node": row,
                        "raw_span": addr,
                        "phone": None,
                        "_col": col,
                        "_cell_texts": cell_texts,
                        "_row_text": row_text,
                    }
                )
    return results


_BRACKETED_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+\s*(?:\[at\]|\(at\))\s*" r"(?:[a-zA-Z0-9.\-]+\s*(?:\[dot\]|\(dot\))\s*)+[a-zA-Z]{2,6}",
    re.IGNORECASE,
)


def _extract_proximity_candidates(text: str, email_re: re.Pattern, ecfg: dict) -> list[dict]:
    results = []
    seen: set[str] = set()
    valid_suffixes = tuple(ecfg.get("valid_suffixes", [".gov.in", ".nic.in"]))

    for m in email_re.finditer(text):
        addr = _truncate_at_known_suffix(m.group(0).lower().strip("."), valid_suffixes)
        if addr in seen:
            continue
        seen.add(addr)
        results.append(
            {
                "address": addr,
                "rung": "proximity_text",
                "context_node": None,
                "raw_span": addr,
                "phone": None,
                "_text_start": m.start(),
                "_text_end": m.end(),
                "_full_text": text,
            }
        )

    # Also capture bracketed obfuscated forms (resolved in stage 4)
    for m in _BRACKETED_EMAIL_RE.finditer(text):
        raw = m.group(0)
        # Tentative address key before resolution — use raw span as placeholder
        placeholder = raw.lower()
        if placeholder in seen:
            continue
        seen.add(placeholder)
        results.append(
            {
                "address": None,  # resolved in stage 4
                "rung": "proximity_text",
                "context_node": None,
                "raw_span": raw,
                "phone": None,
                "_text_start": m.start(),
                "_text_end": m.end(),
                "_full_text": text,
                "_bracketed": True,
            }
        )

    return results


# ── Stage 2: bind_channels ──────────────────────────────────────────────────


def _bind_channels(candidates: list[dict], role_local_parts: set[str], ecfg: dict) -> list[dict]:
    """
    Groups candidates into entity dicts. For each unique email address, keeps
    only the highest-rung candidate. Assigns channel_tag and entity_kind.
    Bracketed candidates (address=None, _bracketed=True) are passed through
    for stage-4 resolution.

    Returns list of entity dicts.
    """
    email_map: dict[str, dict] = {}
    bracketed_pending: list[dict] = []
    phone_candidates: list[dict] = []  # phone-only candidates (no email)

    gov_suffixes = tuple(ecfg.get("valid_suffixes", [".gov.in", ".nic.in"]))

    for c in candidates:
        addr = c.get("address")
        if c.get("_bracketed") and addr is None:
            bracketed_pending.append(c)
            continue
        if addr:
            if addr not in email_map:
                email_map[addr] = c
            else:
                existing_rank = _RUNG_RANK.get(email_map[addr]["rung"], -1)
                new_rank = _RUNG_RANK.get(c["rung"], -1)
                if new_rank > existing_rank:
                    old_phone = email_map[addr].get("phone")
                    email_map[addr] = c
                    if not c.get("phone") and old_phone:
                        email_map[addr] = dict(c, phone=old_phone)
        elif c.get("phone"):
            phone_candidates.append(c)

    entities = []
    for addr, c in email_map.items():
        local_part = addr.split("@")[0].lower()
        domain_part = addr.split("@")[1].lower() if "@" in addr else ""

        if local_part in role_local_parts:
            channel_tag = "role"
            entity_kind = "org"
        elif not domain_part.endswith(gov_suffixes):
            channel_tag = "personal-external"
            entity_kind = "person"
        else:
            channel_tag = "office"
            entity_kind = "person"

        entities.append(dict(c, email=addr, channel_tag=channel_tag, entity_kind=entity_kind))

    # Attach phone-only candidates to entities without a phone.
    # Best-effort: match by DOM proximity (same parent container); fallback to first phone.
    for phone_c in phone_candidates:
        phone_num = phone_c.get("phone")
        phone_rung = phone_c.get("rung", "mailto_tel")
        phone_node = phone_c.get("context_node")
        best_entity = None

        if phone_node is not None:
            # Try to find entity whose context_node shares the closest ancestor
            for entity in entities:
                if entity.get("phone"):
                    continue
                e_node = entity.get("context_node")
                if e_node is not None and _nodes_share_container(phone_node, e_node):
                    best_entity = entity
                    break

        if best_entity is None:
            # Fallback: attach to all entities without a phone
            for entity in entities:
                if not entity.get("phone"):
                    entity["phone"] = phone_num
                    entity["_phone_rung"] = phone_rung
        else:
            best_entity["phone"] = phone_num
            best_entity["_phone_rung"] = phone_rung

    # Append bracketed pending candidates — resolved in stage 4
    entities.extend(bracketed_pending)
    return entities


def _nodes_share_container(node_a, node_b) -> bool:
    """True if both nodes are within the same immediate parent container."""
    try:
        return node_a.parent is not None and node_a.parent is node_b.parent
    except Exception:
        return False


# ── Stage 3: enrich_fields ──────────────────────────────────────────────────


def _enrich_fields(
    entities: list[dict], soup: BeautifulSoup, raw_text: str, source_url: str, pcfg: dict, ecfg: dict
) -> list[dict]:
    """
    Adds person_name, designation, department to each entity.
    Name/designation gated by person.enabled.
    Department defaults to URL-derived; page-vs-domain mismatch sets _dept_mismatch.
    """
    person_enabled = pcfg.get("enabled", True)
    url_dept = _dept_from_url(source_url)
    prox_chars = pcfg.get("proximity_chars", 300)
    # Hoist DOM traversal outside loop — result is identical for all entities
    page_dept = _dept_from_page(soup)
    dept_mismatch = bool(page_dept and url_dept and page_dept.lower() != url_dept.lower())

    for entity in entities:
        entity.setdefault("person_name", None)
        entity.setdefault("designation", None)
        entity.setdefault("department", None)
        entity.setdefault("_dept_mismatch", False)
        entity.setdefault("context_snippet", "")

        rung = entity.get("rung")

        # Build context window
        if rung == "table_block":
            row_text = entity.get("_row_text", "")
            entity["context_snippet"] = row_text[:300]
            if person_enabled:
                col = entity.get("_col", {})
                cell_texts = entity.get("_cell_texts", [])
                entity["person_name"] = _cell_value(cell_texts, col.get("name"))
                entity["designation"] = _clip_designation(_cell_value(cell_texts, col.get("designation")))
                entity["department"] = _cell_value(cell_texts, col.get("department"))

                # Fallback name/designation from row_text
                if not entity["person_name"] and pcfg.get("title_prefixes"):
                    entity["person_name"] = _match_name(row_text, pcfg)
                if not entity["designation"] and pcfg.get("designation_keywords"):
                    entity["designation"] = _match_designation(row_text, pcfg)

        elif rung == "proximity_text":
            start = entity.get("_text_start", 0)
            end = entity.get("_text_end", 0)
            full_text = entity.get("_full_text", raw_text)
            ctx_chars = ecfg.get("context_chars", 200)
            window = full_text[max(0, start - prox_chars) : end + prox_chars]
            context = " ".join(full_text[max(0, start - ctx_chars) : end + ctx_chars].split())
            entity["context_snippet"] = context
            if person_enabled:
                entity["person_name"] = _match_name(window, pcfg)
                entity["designation"] = _match_designation(window, pcfg)

        # Department: prefer table column, fallback to URL-derived
        if not entity.get("department"):
            entity["department"] = url_dept

        if dept_mismatch:
            entity["_dept_mismatch"] = True

    return entities


def _dept_from_url(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        return host.split(".")[0] if host else None
    except Exception:
        return None


def _dept_from_page(soup: BeautifulSoup) -> str | None:
    for el in soup.find_all(itemprop="name"):
        text = el.get_text(strip=True)
        if text:
            return text[:80]
    return None


def _match_name(text: str, pcfg: dict) -> str | None:
    prefixes = pcfg.get("title_prefixes", [])
    if not prefixes:
        return None
    # Horizontal whitespace only ([ \t], not \s) between words — a name
    # must never continue across a line break. Without this, a following
    # line's leading word (e.g. a role like "Scientist" on its own line)
    # gets swallowed into the captured name.
    pat = r"\b(" + "|".join(re.escape(p) for p in prefixes) + r")\b\.?[ \t]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){0,3})"
    m = re.search(pat, text)
    if m:
        return " ".join(f"{m.group(1)} {m.group(2)}".split())
    return None


def _clip_designation(value: str | None, max_chars: int = 120) -> str | None:
    """Cuts a raw designation string at the first sign it has run past a
    job title into embedded contact info (an email, a phone label, a long
    digit run, or a line break) — a keyword/table-column hit only tells us
    WHERE a title starts, never how long it is."""
    if not value:
        return None
    window = value[:max_chars]
    stop = _DESIGNATION_STOP_RE.search(window)
    end = stop.start() if stop else len(window)
    cleaned = " ".join(window[:end].split()).strip(" ,.-")
    return cleaned[:60] if cleaned else None


def _match_designation(text: str, pcfg: dict) -> str | None:
    keywords = pcfg.get("designation_keywords", [])
    if not keywords:
        return None
    pat = r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b"
    m = re.search(pat, text, re.IGNORECASE)
    if not m:
        return None
    return _clip_designation(text[m.start() :])


# ── Stage 4: normalise_spans ────────────────────────────────────────────────


def _normalise_spans(entities: list[dict], ecfg: dict, role_local_parts: set[str] = None) -> list[dict]:
    """
    Guarded de-obfuscation: only on email-shaped candidate spans with bracketed forms.
    Resolves bracketed pending candidates to real emails, then classifies them.
    Never does global text rewrites.
    """
    if role_local_parts is None:
        role_local_parts = set()

    obfuscation = ecfg.get("obfuscation", [])
    email_re = re.compile(
        ecfg.get("regex", r"[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}"),
        re.IGNORECASE,
    )
    gov_suffixes = tuple(ecfg.get("valid_suffixes", [".gov.in", ".nic.in"]))

    resolved_emails: set[str] = {e.get("email") for e in entities if e.get("email")}
    new_entities = []

    for entity in entities:
        if entity.get("rung") != "proximity_text":
            new_entities.append(entity)
            continue
        raw_span = entity.get("raw_span", "")
        if not _BRACKETED_EMAIL_RE.search(raw_span):
            new_entities.append(entity)
            continue
        # Apply bracketed obfuscation pairs to this span only
        resolved = raw_span
        for pattern, replacement in obfuscation:
            resolved = re.sub(pattern, replacement, resolved, flags=re.IGNORECASE)
        m = email_re.search(resolved)
        if not m:
            continue  # drop — could not resolve to valid email
        addr = _truncate_at_known_suffix(m.group(0).lower().strip("."), gov_suffixes)
        if addr in resolved_emails:
            continue  # already have a better-rung candidate
        resolved_emails.add(addr)

        local_part = addr.split("@")[0].lower()
        domain_part = addr.split("@")[1].lower() if "@" in addr else ""
        if local_part in role_local_parts:
            channel_tag, entity_kind = "role", "org"
        elif not domain_part.endswith(gov_suffixes):
            channel_tag, entity_kind = "personal-external", "person"
        else:
            channel_tag, entity_kind = "office", "person"

        new_entities.append(dict(entity, address=addr, email=addr, channel_tag=channel_tag, entity_kind=entity_kind))

    return new_entities


# ── Stage 5: score ──────────────────────────────────────────────────────────


def _score(entities: list[dict], high_rungs: set[str], mid_rungs: set[str]) -> list[dict]:
    """Assigns confidence_band and builds field_provenance JSON."""
    for entity in entities:
        rung = entity.get("rung", "proximity_text")
        # Band is informational only — shows where the email was found.
        # HIGH = from a mailto link or microdata. LOW = from a table or text scan.
        band = "HIGH" if rung in high_rungs else "LOW"

        # Degrade to LOW on dept mismatch
        if entity.get("_dept_mismatch") and band == "HIGH":
            band = "LOW"

        entity["confidence_band"] = band

        name_rung = rung if entity.get("person_name") else None
        desig_rung = rung if entity.get("designation") else None
        dept_rung = (
            rung
            if entity.get("_col", {}).get("department") is not None and entity.get("department")
            else ("url_default" if entity.get("department") else None)
        )
        phone_rung = entity.get("_phone_rung") if entity.get("phone") else None
        provenance = {
            "email": rung,
            "person_name": name_rung,
            "designation": desig_rung,
            "department": dept_rung,
            "phone": phone_rung,
        }
        entity["field_provenance"] = json.dumps({k: v for k, v in provenance.items() if v is not None})

    return entities


# ── Stage 6: flatten_emit ───────────────────────────────────────────────────


def _flatten_emit(entities: list[dict], source_url: str, page_title: str) -> list[Lead]:
    """One flat Lead per email; only email-less entities are skipped.

    Band never drops a lead — an email with no name is still a lead.
    """
    leads = []
    seen_emails: set[str] = set()
    for entity in entities:
        email = entity.get("email")
        if not email or email in seen_emails:
            continue
        seen_emails.add(email)
        leads.append(
            Lead(
                email=email,
                person_name=entity.get("person_name"),
                designation=entity.get("designation"),
                department=entity.get("department"),
                source_url=source_url,
                source_title=page_title,
                context_snippet=entity.get("context_snippet", ""),
                entity_kind=entity.get("entity_kind"),
                phone=entity.get("phone"),
                channel_tag=entity.get("channel_tag"),
                confidence_band=entity.get("confidence_band"),
                field_provenance=entity.get("field_provenance"),
            )
        )
    return leads


# ── Helpers ─────────────────────────────────────────────────────────────────


def _apply_obfuscation(text: str, ecfg: dict) -> str:
    for pattern, replacement in ecfg.get("obfuscation", []):
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


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
    return v[:_MAX_CELL_CHARS] if v else None
