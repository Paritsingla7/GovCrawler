"""Parser enrichment fixes (see PLAN_attribution_and_parser.md):
WI-8 — mailto_tel/microdata candidates get enriched (Tier 1) and recover a
displaced candidate's richer context on an email collision (Tier 2).
WI-7 — bracketed-obfuscated emails are detected/resolved per the
admin-editable `obfuscation` config, not a hardcoded [at]/[dot]-only regex.
"""

from bs4 import BeautifulSoup

from agent.crawler.parser import extract_leads

CONFIG = {
    "email": {
        "enabled": True,
        "regex": r"[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}",
        "valid_suffixes": [".gov.in", ".nic.in", ".res.in", ".ac.in", ".com"],
        "obfuscation": [
            [r"\s*\[at\]\s*", "@"],
            [r"\s*\(at\)\s*", "@"],
            [r"\s*\[dot\]\s*", "."],
            [r"\s*\(dot\)\s*", "."],
            [r"\s*\[hyphen\]\s*", "-"],
            [r"\s*\(hyphen\)\s*", "-"],
        ],
        "context_chars": 200,
    },
    "max_input_chars": 200000,
    "role_local_parts": ["webmaster", "info", "admin", "contact", "support", "helpdesk", "grievance"],
    "confidence": {"high_rungs": ["mailto_tel", "microdata"]},
    "person": {
        "enabled": True,
        "title_prefixes": ["Shri", "Smt", "Dr", "Mr", "Mrs", "Ms", "Prof", "Sh", "Shrimati", "Km"],
        "designation_keywords": ["Secretary", "Director", "Commissioner", "Officer", "Manager", "Chief"],
    },
}

URL = "https://tn.gov.in/contact"


def _leads(html):
    soup = BeautifulSoup(html, "html.parser")
    return extract_leads(soup, URL, CONFIG)


def _by_email(leads, email):
    return next(lead for lead in leads if lead.email == email)


# ── WI-8 Tier 1: mailto_tel candidates were always blank ────────────────────


def test_mailto_enriched_from_its_own_container():
    html = """
    <div>
      <p>Shri Ram Kumar, Director</p>
      <a href="mailto:ram.kumar@tn.gov.in">Email</a>
    </div>
    """
    lead = _by_email(_leads(html), "ram.kumar@tn.gov.in")
    assert lead.person_name == "Shri Ram Kumar"
    assert lead.designation == "Director"
    assert lead.confidence_band == "HIGH"


# ── WI-8 Tier 2: collision — table's richer context recovered for the mailto winner ─


def test_mailto_falls_back_to_table_context_on_collision():
    html = """
    <table>
      <tr><th>Name</th><th>Designation</th><th>Email</th></tr>
      <tr><td>Ram Kumar</td><td>Director</td><td>ram.kumar@tn.gov.in</td></tr>
    </table>
    <div class="footer"><a href="mailto:ram.kumar@tn.gov.in">Email Us</a></div>
    """
    lead = _by_email(_leads(html), "ram.kumar@tn.gov.in")
    # mailto rung wins (highest precedence) but its own container ("Email
    # Us") has no name/designation — must recover the table's.
    assert lead.person_name == "Ram Kumar"
    assert lead.designation == "Director"


def test_table_only_case_unchanged_by_fallback_logic():
    html = """
    <table>
      <tr><th>Name</th><th>Designation</th><th>Email</th></tr>
      <tr><td>Ram Kumar</td><td>Director</td><td>ram.kumar@tn.gov.in</td></tr>
    </table>
    """
    lead = _by_email(_leads(html), "ram.kumar@tn.gov.in")
    assert lead.person_name == "Ram Kumar"
    assert lead.designation == "Director"
    assert lead.confidence_band == "LOW"  # table_block is a non-high rung


# ── WI-7: bracketed de-obfuscation is per-span (Stage 4), not global ────────


def test_bracketed_at_dot_email_resolves():
    html = "<p>Contact: webmaster [at] nic [dot] in for queries.</p>"
    lead = _by_email(_leads(html), "webmaster@nic.in")
    assert lead.channel_tag == "role"


def test_bracketed_hyphen_marker_not_lost():
    # [hyphen] is configured but NOT recognized by the old hardcoded
    # at/dot-only detector — regression guard for the WI-7 rewrite.
    html = "<p>Write to: state[hyphen]desk[at]tn[dot]nic[dot]in for help.</p>"
    lead = _by_email(_leads(html), "state-desk@tn.nic.in")
    assert lead.channel_tag == "office"


def test_plain_and_bracketed_emails_coexist_on_one_page():
    html = """
    <p>Plain: ram.kumar@tn.gov.in</p>
    <p>Obfuscated: web[hyphen]master[at]tn[dot]nic[dot]in</p>
    """
    leads = _leads(html)
    emails = {lead.email for lead in leads}
    assert emails == {"ram.kumar@tn.gov.in", "web-master@tn.nic.in"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK  {name}")
