"""Migration 0028: Lead.email moves from a (job_id, email) unique constraint
to a plain (email) one, matching what save_lead()/bulk_upsert_manual_leads()
already assume (a global lead pool, deduped by email alone). Before that
constraint can be added, any pre-existing duplicate emails across jobs must
be merged — this covers the merge logic (_dedupe_leads) and the constraint
swap itself, each in isolation against a hand-built pre-migration schema."""

import importlib.util
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect as sa_inspect, text

_MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0028_dedupe_lead_email_global.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig0028", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_migration_schema(engine):
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    email VARCHAR NOT NULL,
                    person_name VARCHAR, designation VARCHAR, department VARCHAR,
                    source_url VARCHAR, source_title VARCHAR, context_snippet TEXT,
                    phone VARCHAR, entity_kind VARCHAR, confidence_band VARCHAR,
                    lead_score INTEGER NOT NULL DEFAULT 0, depth INTEGER NOT NULL DEFAULT 0,
                    CONSTRAINT uq_lead_job_email UNIQUE (job_id, email)
                )
                """))
        conn.execute(
            text(
                "CREATE TABLE lead_occurrences (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, job_id INTEGER)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE campaign_emails (id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER, "
                "lead_id INTEGER, recipient_email VARCHAR)"
            )
        )


def test_dedupe_leads_merges_enriches_and_repoints(tmp_path):
    mig = _load_migration()
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    _pre_migration_schema(engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO leads (job_id, email, designation, confidence_band, lead_score) "
                "VALUES (1, 'dup@x.gov.in', 'Officer', 'LOW', 10)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO leads (job_id, email, person_name, confidence_band, lead_score) "
                "VALUES (2, 'dup@x.gov.in', 'Real Name', 'HIGH', 20)"
            )
        )
        conn.execute(text("INSERT INTO leads (job_id, email, lead_score) VALUES (3, 'unique@x.gov.in', 5)"))

        ids = [r.id for r in conn.execute(text("SELECT id FROM leads WHERE email='dup@x.gov.in'")).fetchall()]
        conn.execute(text("INSERT INTO lead_occurrences (lead_id, job_id) VALUES (:l, 1)"), {"l": ids[0]})
        conn.execute(text("INSERT INTO lead_occurrences (lead_id, job_id) VALUES (:l, 2)"), {"l": ids[1]})
        conn.execute(
            text("INSERT INTO campaign_emails (campaign_id, lead_id, recipient_email) VALUES (1, :l, 'dup@x.gov.in')"),
            {"l": ids[0]},
        )

    with engine.begin() as conn:
        merged = mig._dedupe_leads(conn)
        assert merged == 1

        remaining = conn.execute(
            text("SELECT id, person_name, designation, confidence_band FROM leads WHERE email='dup@x.gov.in'")
        ).fetchall()
        assert len(remaining) == 1
        canonical = remaining[0]
        # HIGH beats LOW (upgrade, not overwrite) and the LOW row's designation
        # fills the gap in the canonical (HIGH) row rather than being lost.
        assert canonical.confidence_band == "HIGH"
        assert canonical.person_name == "Real Name"
        assert canonical.designation == "Officer"

        occ_jobs = {
            r.job_id
            for r in conn.execute(text("SELECT job_id FROM lead_occurrences WHERE lead_id = :l"), {"l": canonical.id})
        }
        assert occ_jobs == {1, 2}  # both jobs' occurrences survive, re-pointed at canonical

        ce_lead_ids = {r.lead_id for r in conn.execute(text("SELECT lead_id FROM campaign_emails"))}
        assert ce_lead_ids == {canonical.id}

        still_unique = conn.execute(text("SELECT COUNT(*) FROM leads WHERE email='unique@x.gov.in'")).scalar()
        assert still_unique == 1


def test_dedupe_leads_is_noop_when_no_duplicates(tmp_path):
    mig = _load_migration()
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    _pre_migration_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO leads (job_id, email, lead_score) VALUES (1, 'a@x.gov.in', 1)"))
        conn.execute(text("INSERT INTO leads (job_id, email, lead_score) VALUES (2, 'b@x.gov.in', 1)"))

    with engine.begin() as conn:
        assert mig._dedupe_leads(conn) == 0


def test_upgrade_swaps_job_email_constraint_for_email_only(tmp_path):
    mig = _load_migration()
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    _pre_migration_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO leads (job_id, email, lead_score) VALUES (1, 'a@x.gov.in', 1)"))
        conn.execute(text("INSERT INTO leads (job_id, email, lead_score) VALUES (2, 'b@x.gov.in', 1)"))

    before = sa_inspect(engine).get_unique_constraints("leads")
    assert {"job_id", "email"} == set(before[0]["column_names"])

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        mig.op = Operations(ctx)
        with conn.begin():
            mig.upgrade()

    after = sa_inspect(engine).get_unique_constraints("leads")
    assert any(c["column_names"] == ["email"] for c in after)
    assert not any(set(c["column_names"]) == {"job_id", "email"} for c in after)
