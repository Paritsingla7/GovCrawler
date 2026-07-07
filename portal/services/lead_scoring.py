"""Moved to shared/scoring.py (Phase 0 — shared/ carve-out). Re-exported here
so any straggling imports (docs, external scripts) keep working."""
from shared.scoring import DEFAULT_WEIGHTS, compute_lead_score

__all__ = ["DEFAULT_WEIGHTS", "compute_lead_score"]
