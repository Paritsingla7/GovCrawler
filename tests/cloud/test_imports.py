"""Import-sanity check — catches the class of bug where a function moves
between modules but a caller's import statement doesn't (e.g. the
_normalize_custom_urls ImportError fixed in cloud/api/jobs.py)."""


def test_cloud_api_server_imports():
    import cloud.api.server  # noqa: F401


def test_cloud_api_oauth_imports():
    import cloud.api.oauth  # noqa: F401


def test_cloud_security_oauth_imports():
    import cloud.security.oauth  # noqa: F401
