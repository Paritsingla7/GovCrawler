"""portal.main.load_config's env-override behavior — unverified by anything
until now, despite several Phase 5/6 features depending on it (least-
privilege DB role, VPS dispatcher split, cross-machine resume)."""
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


def test_cross_machine_resume_env_override(monkeypatch):
    monkeypatch.setenv("CROSS_MACHINE_RESUME", "true")
    config = main.load_config()
    assert config["crawler"]["cross_machine_resume"] is True


def test_cross_machine_resume_defaults_off(monkeypatch):
    monkeypatch.delenv("CROSS_MACHINE_RESUME", raising=False)
    config = main.load_config()
    assert config["crawler"]["cross_machine_resume"] is False
