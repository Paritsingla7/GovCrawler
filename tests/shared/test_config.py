"""portal.main.load_config's env-override behavior — unverified by anything
until now, despite several Phase 5/6 features depending on it (least-
privilege DB role, VPS dispatcher split)."""

import portal.main as main


def test_database_url_app_takes_precedence_over_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://superuser@db/govcrawler")
    monkeypatch.setenv("DATABASE_URL_APP", "postgresql://govcrawler_app@db/govcrawler")
    config = main.load_config()
    assert config["database"]["uri"] == "postgresql://govcrawler_app@db/govcrawler"


def test_database_url_used_when_app_url_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL_APP", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://superuser@db/govcrawler")
    config = main.load_config()
    assert config["database"]["uri"] == "postgresql://superuser@db/govcrawler"


def test_dispatch_mode_env_override(monkeypatch):
    monkeypatch.setenv("DISPATCH_MODE", "external")
    config = main.load_config()
    assert config["dispatch"]["mode"] == "external"


def test_oauth_env_overrides(monkeypatch):
    monkeypatch.setenv("OAUTH_REDIRECT_BASE_URL", "https://cloud.example.com")
    monkeypatch.setenv("OAUTH_MS_CLIENT_ID", "ms-client-id")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_SECRET", "google-client-secret")
    config = main.load_config()
    assert config["oauth"]["redirect_base_url"] == "https://cloud.example.com"
    assert config["oauth"]["microsoft"]["client_id"] == "ms-client-id"
    assert config["oauth"]["google"]["client_id"] == "google-client-id"
    assert config["oauth"]["google"]["client_secret"] == "google-client-secret"
