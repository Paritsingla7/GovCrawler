"""
Shared draft-generation logic for campaign email staging — used by both
POST /api/campaigns (initial draft generation) and POST /api/campaigns/{id}/emails
(adding more leads to an existing campaign).
"""

import logging

from jinja2 import Template

log = logging.getLogger(__name__)


def render_template_string(template_str: str, **kwargs) -> str:
    """Render a Jinja2 template string with the given variables.
    Pre-validated templates should never fail here, but we handle it gracefully."""
    try:
        return Template(template_str).render(**kwargs)
    except Exception as e:
        log.warning(f"Template render failed: {e}")
        return template_str  # Fallback to raw string


def is_blacklisted(email: str, blacklisted_emails: set[str], blacklisted_domains: set[str]) -> bool:
    """Case-insensitive email match, plus domain-wide suppression (issue #58): the
    `blacklist.domain` column was stored on every insert but never read anywhere, so
    blocking an entire domain silently did nothing — only the exact email mattered."""
    email = email.lower()
    domain = email.split("@")[1] if "@" in email else ""
    return email in blacklisted_emails or domain in blacklisted_domains


def render_draft_emails(
    leads: list[dict],
    template: dict,
    blacklisted_emails: set[str],
    lead_id_by_email: dict[str, int],
    blacklisted_domains: set[str] = frozenset(),
    exclude_emails: set[str] = frozenset(),
) -> tuple[list[dict], int, int]:
    """
    Filters leads against the blacklist (by exact email, case-insensitively, and by
    domain) and an optional exclude set (e.g. recipients already staged in the
    campaign), renders the template's subject/body per lead, and builds email dicts
    ready for db.bulk_create_campaign_emails().

    Returns (email_dicts, blacklisted_count, excluded_count).
    """
    filtered = [lead for lead in leads if not is_blacklisted(lead["email"], blacklisted_emails, blacklisted_domains)]
    blacklisted_count = len(leads) - len(filtered)

    excluded_count = sum(1 for lead in filtered if lead["email"] in exclude_emails)
    if exclude_emails:
        filtered = [lead for lead in filtered if lead["email"] not in exclude_emails]

    email_dicts = []
    for lead in filtered:
        # Detect missing template variables before applying fallbacks
        missing = []
        if not lead.get("person_name"):
            missing.append("name")
        if not lead.get("designation"):
            missing.append("designation")

        # Subject uses clean fallbacks (no placeholder markers)
        subject_vars = {
            "name": lead.get("person_name") or "Official",
            "designation": lead.get("designation") or "",
        }
        # Body uses visible [MISSING: field] markers so the user knows what to fix
        body_vars = {
            "name": lead.get("person_name") or "[MISSING: name]",
            "designation": lead.get("designation") or "[MISSING: designation]",
        }

        lead_id = lead_id_by_email.get(lead["email"])
        if lead_id is None:
            continue  # Safety: skip if we can't resolve the FK

        email_dicts.append(
            {
                "lead_id": lead_id,
                "recipient_email": lead["email"],
                "subject": render_template_string(template["subject"], **subject_vars),
                "body": render_template_string(template["raw_body"], **body_vars),
                "is_selected": len(missing) == 0,  # deselect emails with missing data
                "missing_fields": ",".join(missing) if missing else None,
            }
        )

    return email_dicts, blacklisted_count, excluded_count
