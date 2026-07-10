"""End-to-end coverage for Plan 1 (PLAN_attribution_and_parser.md): a lead's
snapshot_id is resolved from its actual source_url at save time (WI-4), and a
lead that resolves to no catalog domain surfaces as "Unknown" rather than
being misread as manual (WI-5) — against a real SQLite Database, not mocks.
"""

from cryptography.fernet import Fernet

from cloud.db.database import Database


def _make_db(tmp_path) -> Database:
    config = {
        "database": {"uri": f"sqlite:///{tmp_path}/test.db"},
        "auth": {"credential_enc_key": Fernet.generate_key().decode()},
    }
    return Database(config, config_path=tmp_path / "config.yaml")


def _seed_domain(db, main_url, category_code="cat") -> dict:
    domain_id = db.upsert_domain(
        category_code=category_code,
        category_title="Category",
        state="TN",
        org_type="dept",
        org_type_title="Department",
        title=main_url,
        main_url=main_url,
        contact_url=None,
    )
    return db.get_domains_by_ids([domain_id])[0]


def test_lead_reattributed_to_discovered_domain(tmp_path):
    db = _make_db(tmp_path)
    seed_domain = _seed_domain(db, "https://tn.gov.in")
    discovered_domain = _seed_domain(db, "https://rera.tn.gov.in")

    job_id = db.create_job(domain_ids=[seed_domain["id"], discovered_domain["id"]])
    seed_snapshot_id = db.create_crawl_snapshot(job_id, seed_domain, is_seed=True)

    # Crawl started at tn.gov.in (seed) but followed a link into
    # rera.tn.gov.in and captured a lead there — save_lead must attribute it
    # to rera.tn.gov.in, not the inherited seed snapshot.
    db.save_lead(
        job_id=job_id,
        snapshot_id=seed_snapshot_id,
        email="officer@rera.tn.gov.in",
        person_name="Officer",
        designation=None,
        department=None,
        source_url="https://rera.tn.gov.in/contact",
        source_title="",
        context_snippet="",
        channel_tag="office",
    )

    leads, _ = db.get_leads(job_ids=[job_id])
    lead = next(lead for lead in leads if lead["email"] == "officer@rera.tn.gov.in")
    assert lead["is_manual"] is False
    assert lead["domain_state"] == "TN"

    seeds = db.get_crawl_snapshots(job_id)  # seeds_only=True default
    assert len(seeds) == 1 and seeds[0]["source_domain_id"] == seed_domain["id"]

    all_snaps = db.get_crawl_snapshots(job_id, seeds_only=False)
    assert len(all_snaps) == 2
    discovered_snap = next(s for s in all_snaps if s["source_domain_id"] == discovered_domain["id"])
    assert discovered_snap["is_seed"] is False


def test_lead_off_catalog_is_unknown_not_manual(tmp_path):
    db = _make_db(tmp_path)
    seed_domain = _seed_domain(db, "https://tn.gov.in")
    job_id = db.create_job(domain_ids=[seed_domain["id"]])
    seed_snapshot_id = db.create_crawl_snapshot(job_id, seed_domain, is_seed=True)

    db.save_lead(
        job_id=job_id,
        snapshot_id=seed_snapshot_id,
        email="contact@unrelated.example.com",
        person_name=None,
        designation=None,
        department=None,
        source_url="https://unrelated.example.com/page",
        source_title="",
        context_snippet="",
        channel_tag="personal-external",
    )

    leads, _ = db.get_leads(job_ids=[job_id])
    lead = next(lead for lead in leads if lead["email"] == "contact@unrelated.example.com")
    assert lead["is_manual"] is False  # NOT badged manual just because snapshot is null
    assert lead["domain_state"] == "Unknown"

    # Not editable as manual — it's an unattributed CRAWLED lead, not manual.
    assert db.update_lead(lead["id"], {"manual_state": "Kerala"}) == "not_manual"

    states = db.get_lead_states(job_ids=[job_id])
    assert "Unknown" in states
    categories = db.get_lead_categories(job_ids=[job_id])
    assert any(c["code"] == "__unknown__" and c["count"] == 1 for c in categories)

    # Filtering on the Unknown sentinel returns exactly this lead.
    filtered_ids = db.get_lead_ids(job_ids=[job_id], states=["Unknown"])
    assert filtered_ids == [lead["id"]]
    # Filtering on a real state excludes it.
    assert db.get_lead_ids(job_ids=[job_id], states=["TN"]) == []


def test_save_lead_upgrades_confidence_band_but_never_downgrades(tmp_path):
    """WI-9 Bug B: a re-capture with objectively stronger evidence (a real
    mailto: link, HIGH, where an earlier pass only scraped page text, LOW)
    must upgrade the stored band and recompute lead_score — not just
    fill-if-null. A worse re-capture must never downgrade it."""
    db = _make_db(tmp_path)
    seed_domain = _seed_domain(db, "https://tn.gov.in")
    job_id = db.create_job(domain_ids=[seed_domain["id"]])
    seed_snapshot_id = db.create_crawl_snapshot(job_id, seed_domain, is_seed=True)

    def _capture(confidence_band):
        db.save_lead(
            job_id=job_id,
            snapshot_id=seed_snapshot_id,
            email="officer@tn.gov.in",
            person_name=None,
            designation=None,
            department=None,
            source_url="https://tn.gov.in/contact",
            source_title="",
            context_snippet="",
            channel_tag="office",
            confidence_band=confidence_band,
            field_provenance=f'{{"email": "{confidence_band}"}}',
        )

    _capture("LOW")
    low_score = db.get_leads(job_ids=[job_id])[0][0]["lead_score"]

    _capture("HIGH")
    lead = db.get_leads(job_ids=[job_id])[0][0]
    assert lead["confidence_band"] == "HIGH"
    assert lead["field_provenance"] == '{"email": "HIGH"}'
    assert lead["lead_score"] > low_score  # recomputed off the better band

    _capture("LOW")  # a worse re-capture must NOT downgrade
    lead = db.get_leads(job_ids=[job_id])[0][0]
    assert lead["confidence_band"] == "HIGH"


def test_manual_lead_unaffected_by_unknown_sentinel(tmp_path):
    db = _make_db(tmp_path)
    seed_domain = _seed_domain(db, "https://tn.gov.in")
    job_id = db.create_job(domain_ids=[seed_domain["id"]])

    db.bulk_upsert_manual_leads(job_id, [{"email": "manual@example.com", "name": "Manual Person"}])

    leads, _ = db.get_leads(job_ids=[job_id])
    lead = next(lead for lead in leads if lead["email"] == "manual@example.com")
    assert lead["is_manual"] is True

    categories = db.get_lead_categories(job_ids=[job_id])
    assert not any(c["code"] == "__unknown__" for c in categories)  # manual lead isn't "unattributed"
    assert db.update_lead(lead["id"], {"manual_state": "Kerala"}) is True
