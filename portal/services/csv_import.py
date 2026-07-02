"""CSV parsing for manually-supplied contacts (custom leads / test-campaign dummy leads)."""

import csv
import io
import re

MAX_ROWS = 2000

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_HEADER_ALIASES = {
    "email": {"email", "e-mail", "email address", "mail"},
    "name": {"name", "full name", "person name", "person_name", "contact name"},
    "designation": {"designation", "title", "job title", "role"},
    "department": {"department", "dept"},
    "phone": {"phone", "phone number", "mobile", "mobile number", "contact number"},
}

TEMPLATE_HEADERS = ["name", "email", "designation", "department", "phone"]
TEMPLATE_SAMPLE_ROW = [
    "Jane Doe", "jane.doe@example.gov.in", "Under Secretary", "Ministry of Example", "9876543210",
]


def _canonical_field(header: str) -> str | None:
    key = header.strip().lower()
    for field, aliases in _HEADER_ALIASES.items():
        if key in aliases:
            return field
    return None


def parse_contacts_csv(content: bytes) -> tuple[list[dict], list[dict]]:
    """Parse a CSV of contacts into normalized rows.

    Returns (rows, skipped). Each row has keys email/name/designation/department/phone
    (missing optional fields are None) plus the source "row" line number. Each skipped
    entry is {"row": int, "email": str | None, "reason": str}.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    field_map = {
        header: canonical
        for header in (reader.fieldnames or [])
        if (canonical := _canonical_field(header))
    }

    rows: list[dict] = []
    skipped: list[dict] = []
    seen_emails: set[str] = set()

    for line_no, raw_row in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            skipped.append({
                "row": line_no, "email": raw_row.get("email"),
                "reason": f"row limit exceeded (max {MAX_ROWS} rows)",
            })
            continue

        normalized = {
            canonical: value.strip()
            for header, value in raw_row.items()
            if (canonical := field_map.get(header)) and value
        }

        email = (normalized.get("email") or "").lower()
        if not email:
            skipped.append({"row": line_no, "email": None, "reason": "missing email"})
            continue
        if not _EMAIL_RE.match(email):
            skipped.append({"row": line_no, "email": email, "reason": "invalid email format"})
            continue
        if email in seen_emails:
            skipped.append({"row": line_no, "email": email, "reason": "duplicate email in file"})
            continue
        seen_emails.add(email)

        rows.append({
            "row": line_no,
            "email": email,
            "name": normalized.get("name"),
            "designation": normalized.get("designation"),
            "department": normalized.get("department"),
            "phone": normalized.get("phone"),
        })

    return rows, skipped


def build_template_csv() -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(TEMPLATE_SAMPLE_ROW)
    return output.getvalue()
