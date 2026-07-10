"""max_links_per_page is stored inside the crawl_policy blob in the
`app_settings` JSON column. JSON (and SQLAlchemy's JSON type) only has string
keys, so a value saved with int keys (0, 1, 2) reads back with string keys
("0", "1", "2") — an int-keyed .get() at read time then always misses and
silently reverts to the endpoint's hardcoded default, even though a real
value was saved. GET/POST /api/config must agree on string keys throughout."""

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud.api import config as config_api
from cloud.api.deps import CurrentUser, get_config as get_app_config, get_config_path, get_current_user, get_db
from cloud.db.database import Database


def _make_db(tmp_path) -> Database:
    config = {
        "database": {"uri": f"sqlite:///{tmp_path}/test.db"},
        "auth": {"credential_enc_key": Fernet.generate_key().decode()},
    }
    return Database(config, config_path=tmp_path / "config.yaml")


def _base_local_cfg():
    return {
        "crawler": {
            "workers": 4,
            "per_url_timeout": 10,
            "playwright_timeout": 20,
            "js_settle_time": 1.0,
            "httpx_first": True,
            "playwright_fallback": True,
        }
    }


def _make_client(db, c, cfg_path):
    app = FastAPI()
    app.include_router(config_api.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=1, email="a@a.com", is_admin=True)
    app.dependency_overrides[get_app_config] = lambda: c
    app.dependency_overrides[get_config_path] = lambda: cfg_path
    return TestClient(app)


def test_max_links_per_page_survives_save_then_reload(tmp_path):
    db = _make_db(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    c = _base_local_cfg()
    client = _make_client(db, c, cfg_path)

    resp = client.post(
        "/api/config",
        json={"max_links_per_page_0": 99, "max_links_per_page_1": 42, "max_links_per_page_default": 7},
    )
    assert resp.status_code == 200

    # Simulates a fresh process picking the policy back up from app_settings
    # (a real crawl worker reload, not the same in-memory dict this request
    # just mutated).
    reloaded = client.get("/api/config").json()
    assert reloaded["max_links_per_page_0"] == 99
    assert reloaded["max_links_per_page_1"] == 42
    assert reloaded["max_links_per_page_default"] == 7

    # And the raw stored policy really does have string keys, confirming
    # this isn't passing by accident.
    policy = db.get_crawl_policy()
    assert set(policy["crawler"]["max_links_per_page"].keys()) >= {"0", "1", "default"}
