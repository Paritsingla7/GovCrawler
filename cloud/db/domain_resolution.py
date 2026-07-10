"""Pure resolution of a lead's source_url to a catalog domain — no DB access.

See PLAN_attribution_and_parser.md Plan 1 WI-2. Used by Database.save_lead
(WI-4) to attribute a lead to the domain its source_url actually belongs to,
rather than blindly inheriting whichever seed the crawl started from.
"""

from urllib.parse import urlparse


def resolve_domain_for_url(source_url: str, netloc_map: dict, target_suffixes: list) -> dict | None:
    """Walks from the exact host up through parent domains looking for a
    catalog match, stopping before any `target_suffixes` entry so a bare
    `gov.in`/`nic.in` can never itself count as a match.

    `netloc_map`: {www-stripped lowercased netloc -> domain_dict}, from
    Database._get_netloc_domain_map (WI-1). `target_suffixes`: e.g.
    [".gov.in", ".nic.in"], from Database.get_crawl_policy()["crawler"].
    """
    try:
        host = urlparse(source_url).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        return None
    if not host:
        return None
    public = {s.lstrip(".") for s in (target_suffixes or [])}
    labels = host.split(".")
    while len(labels) >= 2:
        candidate = ".".join(labels)
        if candidate in public:  # never match a bare public suffix
            break
        hit = netloc_map.get(candidate)
        if hit:
            return hit
        labels = labels[1:]  # strip leftmost label, walk up
    return None
