"""bulk_save_leads() used to call save_lead(**item) via a bare list
comprehension — one malformed item (e.g. a stale-agent payload missing a
required field) raised out of the comprehension, which crashed the whole
/api/coordination/.../leads request and, on the agent side, marked every
other (perfectly good) item in that same 100-row outbox batch as failed too.
Each item must now be isolated so one poison row can't take its batch-mates
down with it."""

from cryptography.fernet import Fernet

from cloud.db.database import Database


def _make_db(tmp_path) -> Database:
    config = {
        "database": {"uri": f"sqlite:///{tmp_path}/test.db"},
        "auth": {"credential_enc_key": Fernet.generate_key().decode()},
    }
    return Database(config, config_path=tmp_path / "config.yaml")


def _good_item(email):
    return {
        "job_id": 1,
        "snapshot_id": None,
        "email": email,
        "person_name": None,
        "designation": None,
        "department": None,
        "source_url": "http://x.gov.in/contact",
        "source_title": None,
        "context_snippet": "",
    }


def test_one_malformed_item_does_not_abort_the_rest(tmp_path):
    db = _make_db(tmp_path)
    items = [
        _good_item("a@x.gov.in"),
        {"job_id": 1, "snapshot_id": None, "email": "b@x.gov.in"},  # missing required kwargs -> TypeError
        _good_item("c@x.gov.in"),
    ]

    results = db.bulk_save_leads(items)

    assert results == [True, False, True]
    leads = db.get_all_leads_for_export(job_ids=[1])
    assert {lead["email"] for lead in leads} == {"a@x.gov.in", "c@x.gov.in"}
