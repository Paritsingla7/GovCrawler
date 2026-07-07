"""0-100 lead score: email trust band + name + designation, with phone as a reserved top slice.

Manual leads (channel_tag == "manual") are never numerically scored — the UI
shows a "MANUAL" tag instead of a number, so 0 is a safe sentinel that also
falls out of any `min_score >= 1` filter for free.
"""

DEFAULT_WEIGHTS = {
    "email_high": 20,  # confidence_band == HIGH (mailto/microdata)
    "email_low": 10,  # everything else (table/proximity-text scrape)
    "person_name": 40,
    "designation": 30,
    "phone": 10,  # reserved top slice: base fields cap at 90 without it
}


def compute_lead_score(fields: dict, confidence_band: str | None = None,
                       channel_tag: str | None = None,
                       weights: dict = DEFAULT_WEIGHTS) -> int:
    if channel_tag == "manual":
        return 0

    score = 0
    if str(fields.get("email") or "").strip():
        score += weights["email_high"] if confidence_band == "HIGH" else weights["email_low"]
    if str(fields.get("person_name") or "").strip():
        score += weights["person_name"]
    if str(fields.get("designation") or "").strip():
        score += weights["designation"]
    if str(fields.get("phone") or "").strip():
        score += weights["phone"]
    return score
