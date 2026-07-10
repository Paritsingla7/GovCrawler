# Implementation Plan — Lead Attribution + Parser Enrichment

> **Self-contained brief for the implementer.** You do not need prior conversation context — everything
> needed is below. Always re-grep locations before editing; code shifts.

---

## Status: Plan 1 (attribution) and Plan 2 WI-7/WI-8 (parser) are DONE

Verified against the working tree (all uncommitted, on `bugfix/parser-enrichment-fixes`):

- **Domain attribution at save time** — `cloud/db/domain_resolution.py` (netloc-walk resolver),
  `cloud/db/mixins/domain_mixin.py` (cached netloc→domain map, invalidated on every catalog write),
  `cloud/db/mixins/crawl_snapshot_mixin.py` (`is_seed` flag + `get_crawl_snapshots(seeds_only=)`),
  `alembic/versions/0025_add_snapshot_is_seed.py`, `cloud/db/mixins/lead_mixin.py`'s `save_lead()`
  (resolves `source_url` and re-attributes on mismatch).
- **"Unknown" for unattributed crawled leads** — `is_manual`/`domain_state`/`update_lead`'s edit gate
  all now key off `channel_tag == "manual"` (not snapshot-null); `get_lead_states/_categories/_org_types`
  append an `UNKNOWN`/`__unknown__` sentinel entry when unattributed leads exist. Frontend needs no
  change — the leads-page filter dropdowns (`frontend/agent/static/js/leads.js`,
  `frontend/cloud/static/js/leads.js`) render whatever the facet API returns; no hardcoded allow-list.
- **Backfill script** — `scripts/backfill_lead_attribution.py` exists and is idempotent. **Still an open
  ops step, not code**: it has to actually be *run* against the real DB once this branch ships (copy
  first, spot-check, per its own runbook docstring). Not a coding task — just don't forget it at deploy.
- **Parser WI-7** (dead Stage-4 / de-obfuscation) — global `_apply_obfuscation` call removed;
  `_build_bracketed_email_re()` now derives the detector from the configured `obfuscation` list instead
  of a hardcoded `[at]/[dot]`-only regex, closing the `[hyphen]`-marker regression risk the original plan
  flagged.
- **Parser WI-8** (mailto/microdata never enriched) — Tier 1 (own-container name/designation match) and
  Tier 2 (fallback context carried from a rung-displaced candidate) both implemented in
  `_bind_channels`/`_enrich_fields`.
- **Tests** — `tests/agent/test_parser.py`, `tests/cloud/test_domain_resolution.py`,
  `tests/cloud/test_lead_attribution.py` cover all of the above, including the hyphen-marker regression
  case and the table/mailto collision case.

Nothing further needed on the above unless review turns up a bug in the implementation itself.

---

## Remaining work — WI-9 (new, in scope for this same branch)

### WI-9 — Confidence band is stuck at 2 bands, and `save_lead` never upgrades it

**Where this came from:** independently surfaced by a codebase audit (GitHub issue #58, "Part 2 §3"),
cross-checked against the current code — both parts are real and unfixed.

**Bug A — `mid_rungs` is dead config.** `agent/crawler/parser.py`'s `_score()` (grep `def _score`)
receives `high_rungs` and `mid_rungs` but only branches on `high_rungs`:
```python
band = "HIGH" if rung in high_rungs else "LOW"
```
Every rung not in `high_rungs` (`table_block`, `proximity_text` — but also anything else) collapses to
`LOW`, so the configured 3-tier model (`high_rungs`/`mid_rungs`/implicit low) is really 2-tier in
practice. `mid_rungs` is read from config (`extract_leads`, grep `mid_rungs = set(conf_cfg.get(...`) and
passed all the way into `_score` for nothing.

**Fix:** give `mid_rungs` an actual middle band — e.g. `band = "HIGH" if rung in high_rungs else ("MID" if
rung in mid_rungs else "LOW")`. **This changes the `confidence_band` value space from `{HIGH, LOW}` to
`{HIGH, MID, LOW}`** — check every consumer before shipping:
- `cloud/db/mixins/lead_mixin.py` — anywhere `confidence_band` is compared/branched (grep
  `confidence_band`), including `shared/scoring.py`'s `compute_lead_score` (grep for how it uses
  `confidence_band` to add `email_high`/`email_low` points — a `MID` value falling through to neither
  branch would silently zero out email-confidence scoring for every mid-rung lead).
- `.docs/database-schema.md` / `.docs/configuration.md` — update the documented band values.
- Frontend — anywhere `HIGH`/`LOW` badges are rendered (`frontend/*/static/js/leads.js`, grep
  `confidence_band`) needs a `MID` case or it'll render as a falsy/blank badge.

**Bug B — enrich-on-conflict never upgrades the band.** `save_lead`'s existing-email branch
(`_ENRICHABLE_FIELDS` loop, grep `for field in _ENRICHABLE_FIELDS`) only fills a field when
`not getattr(existing, field)` — i.e. **null-fill only**. `confidence_band` is never null once first set,
so a later HIGH (or now MID) capture of the same email can never upgrade an existing LOW band, even
though the new capture is objectively better evidence. `.docs/database-schema.md`/`resilience.md` already
carry a correction flag for this (issue #58) — this WI is the actual code fix.

**Fix:** in the enrich branch, special-case `confidence_band` (and `field_provenance`, which encodes it)
to upgrade-if-better instead of fill-if-null: compare rung rank (reuse `agent/crawler/parser.py`'s
`_RUNG_RANK`/band ordering concept, or a small `HIGH > MID > LOW` rank map cloud-side — `confidence_band`
is a plain string on the DB row, the rung itself isn't stored) and overwrite when the new value outranks
the existing one. When the band changes, recompute `lead_score` (the existing `changed = True` path
already does this for other enrichable fields — reuse it, don't duplicate the `compute_lead_score` call).

**Tests:**
1. `_score` with a `table_block`/`proximity_text` rung in `mid_rungs` → asserts `"MID"`, not `"LOW"`.
2. `compute_lead_score`/whatever reads `confidence_band` handles `"MID"` explicitly (not just `HIGH`/else).
3. `save_lead`: seed an existing lead with `confidence_band="LOW"`, save a second capture of the same
   email with `confidence_band="HIGH"` → assert the existing row is upgraded to `HIGH` and `lead_score`
   is recomputed. Also assert a HIGH→LOW second capture does **not** downgrade (only upgrades).

### Explicitly OUT of this branch
- **P4#6 (custom role CRUD / `roles.manage`)** — real, and flagged as "must-add regardless" — but it's an
  auth/RBAC feature, unrelated to parser/attribution code. Tracked as its own branch in
  `BRANCH_PLAN_fable_58.md` (`chore/dead-code-and-half-built-58`, promoted to a `feat/role-crud` branch).
  Do not pull it into this branch.
- A `stay_on_seed_domains` crawler knob — separate feature, lower priority.

---

## How to verify
- **Parser:** pure functions — feed repro HTML strings to `extract_leads(...)` and assert on the
  returned `Lead` fields (see `tests/agent/test_parser.py` for the existing style).
- **Attribution / scoring:** run `python -m portal serve` (SQLite); call `Database.save_lead` directly
  or via `POST /api/coordination/jobs/{id}/leads` with two captures of the same email at different bands
  and assert the stored `confidence_band`/`lead_score`.
