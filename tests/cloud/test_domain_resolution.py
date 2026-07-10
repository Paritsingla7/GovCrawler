from cloud.db.domain_resolution import resolve_domain_for_url

TARGET_SUFFIXES = [".gov.in", ".nic.in"]

NETLOC_MAP = {
    "tn.gov.in": {"id": 1, "title": "Tamil Nadu Govt"},
    "rera.tn.gov.in": {"id": 2, "title": "TN RERA"},
    "mospi.nic.in": {"id": 3, "title": "MOSPI"},
}


def test_exact_catalog_host_matches():
    assert resolve_domain_for_url("https://tn.gov.in/page", NETLOC_MAP, TARGET_SUFFIXES)["id"] == 1


def test_www_prefix_stripped():
    assert resolve_domain_for_url("https://www.tn.gov.in/page", NETLOC_MAP, TARGET_SUFFIXES)["id"] == 1


def test_trailing_slash_and_path_ignored():
    assert resolve_domain_for_url("https://tn.gov.in/", NETLOC_MAP, TARGET_SUFFIXES)["id"] == 1


def test_port_in_netloc_ignored():
    assert resolve_domain_for_url("https://tn.gov.in:8080/page", NETLOC_MAP, TARGET_SUFFIXES)["id"] == 1


def test_subdomain_matches_itself_first():
    assert resolve_domain_for_url("https://rera.tn.gov.in/contact", NETLOC_MAP, TARGET_SUFFIXES)["id"] == 2


def test_subdomain_whose_parent_is_in_catalog():
    assert resolve_domain_for_url("https://forms.tn.gov.in/x", NETLOC_MAP, TARGET_SUFFIXES)["id"] == 1


def test_subdomain_whose_parent_is_not_in_catalog_returns_none():
    assert resolve_domain_for_url("https://forms.unknown.nic.in/x", NETLOC_MAP, TARGET_SUFFIXES) is None


def test_bare_public_suffix_never_matches():
    # A netloc_map that (erroneously) contains the bare suffix itself must
    # still never be returned as a match.
    poisoned_map = dict(NETLOC_MAP, **{"gov.in": {"id": 99}})
    assert resolve_domain_for_url("https://random.gov.in/x", poisoned_map, TARGET_SUFFIXES) is None


def test_no_target_suffixes_stops_at_two_labels():
    assert resolve_domain_for_url("https://random.gov.in/x", NETLOC_MAP, []) is None


def test_unparseable_or_empty_host_returns_none():
    assert resolve_domain_for_url("not a url", NETLOC_MAP, TARGET_SUFFIXES) is None
