"""Crawler/extraction settings endpoints (GET/POST /api/config). See
.docs/configuration.md.

Fields split across two backends (plan.md §19.1 Phase 8 / §3.2), transparently
to the frontend — the wire shape is unchanged either way:
  - machine-local runtime (workers, timeouts, fetch-strategy toggles) -> config.yaml
  - crawl policy (depth/rate limits, filters, extraction rules, lead-score
    weights) -> the cloud `app_settings` table, via Database.get_crawl_policy()/
    set_app_setting()
"""

import copy
import os
import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, Request

from .deps import CurrentUser, client_ip, get_config as get_app_config, get_config_path, get_db, require
from ..db import Database

router = APIRouter(tags=["config"])


@router.get("/api/config")
async def get_config(c: dict = Depends(get_app_config), db: Database = Depends(get_db)):
    policy = db.get_crawl_policy()
    crawler = policy.get("crawler", {})
    extraction = policy.get("extraction", {})
    weights = policy.get("lead_score", {}).get("weights", {})
    return {
        # Machine-local (config.yaml)
        "workers": c["crawler"]["workers"],
        "per_url_timeout": c["crawler"]["per_url_timeout"],
        "httpx_first": c["crawler"].get("httpx_first", True),
        "playwright_fallback": c["crawler"].get("playwright_fallback", True),
        "playwright_timeout": c["crawler"]["playwright_timeout"],
        "js_settle_time": c["crawler"]["js_settle_time"],
        # Policy (app_settings)
        "max_depth": crawler.get("max_depth", 4),
        "recrawl_days": crawler.get("recrawl_days", 30),
        "request_delay": crawler.get("request_delay", 1.5),
        "email_enabled": extraction.get("email", {}).get("enabled", True),
        "email_context_chars": extraction.get("email", {}).get("context_chars", 200),
        "person_enabled": extraction.get("person", {}).get("enabled", True),
        "person_proximity_chars": extraction.get("person", {}).get("proximity_chars", 300),
        # Lead-score weights (policy) — API-only for now, no Settings UI yet
        "weight_email_high": weights.get("email_high", 20),
        "weight_email_low": weights.get("email_low", 10),
        "weight_person_name": weights.get("person_name", 40),
        "weight_designation": weights.get("designation", 30),
        "weight_phone": weights.get("phone", 10),
        # Arrays (policy)
        "target_suffixes": "\n".join(crawler.get("target_suffixes", [])),
        "priority_keywords": "\n".join(crawler.get("priority_keywords", [])),
        "skip_extensions": "\n".join(crawler.get("skip_extensions", [])),
        "valid_suffixes": "\n".join(extraction.get("email", {}).get("valid_suffixes", [])),
        "title_prefixes": "\n".join(extraction.get("person", {}).get("title_prefixes", [])),
        "designation_keywords": "\n".join(extraction.get("person", {}).get("designation_keywords", [])),
        # Dictionary (policy) — keys are always strings: this dict round-trips
        # through the `app_settings` JSON column, which (like JSON itself) only
        # has string keys, so a write with int keys reads back with string keys
        # and int-keyed .get() calls would always silently miss to the default.
        "max_links_per_page_0": crawler.get("max_links_per_page", {}).get("0", 30),
        "max_links_per_page_1": crawler.get("max_links_per_page", {}).get("1", 15),
        "max_links_per_page_2": crawler.get("max_links_per_page", {}).get("2", 8),
        "max_links_per_page_default": crawler.get("max_links_per_page", {}).get("default", 5),
        # Read-only (policy)
        "user_agent": crawler.get("user_agent", ""),
        "js_indicators": "\n".join(crawler.get("js_indicators", [])),
        "email_regex": extraction.get("email", {}).get("regex", ""),
        "email_obfuscation": yaml.dump(extraction.get("email", {}).get("obfuscation", []), default_flow_style=False),
        # Pagination (Story #9, policy) — enabled + the two numeric caps are
        # editable via POST /api/config below. text_signals/param_signals stay
        # display-only here; edit via a direct app_settings write to change those lists.
        "pagination_enabled": crawler.get("pagination", {}).get("enabled", False),
        "pagination_max_pages": crawler.get("pagination", {}).get("max_pagination_pages", 50),
        "pagination_max_chain_children": crawler.get("pagination", {}).get("max_chain_children", 100),
        "pagination_text_signals": "\n".join(crawler.get("pagination", {}).get("text_signals", [])),
        "pagination_param_signals": "\n".join(crawler.get("pagination", {}).get("param_signals", [])),
    }


@router.post("/api/config")
async def save_config(
    body: dict,
    background_tasks: BackgroundTasks,
    request: Request,
    c: dict = Depends(get_app_config),
    config_path=Depends(get_config_path),
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("settings.manage")),
):
    # ── Machine-local (config.yaml) — built here, written after app_settings
    # below succeeds (see that section's comment for why the order matters).
    cfg = copy.deepcopy(c)

    local_int_keys = {"workers", "per_url_timeout", "playwright_timeout"}
    local_float_keys = {"js_settle_time"}
    local_bool_keys = {"httpx_first", "playwright_fallback"}

    for k in local_int_keys:
        if k in body:
            cfg["crawler"][k] = int(body[k])
    for k in local_float_keys:
        if k in body:
            cfg["crawler"][k] = float(body[k])
    for k in local_bool_keys:
        if k in body:
            cfg["crawler"][k] = bool(body[k])

    # ── Policy (app_settings) ────────────────────────────────────────────────
    policy = copy.deepcopy(db.get_crawl_policy())
    crawler = policy.setdefault("crawler", {})
    extraction = policy.setdefault("extraction", {})
    weights = policy.setdefault("lead_score", {}).setdefault("weights", {})
    old_weights = dict(weights)

    policy_int_keys = {"max_depth", "recrawl_days"}
    policy_float_keys = {"request_delay"}
    for k in policy_int_keys:
        if k in body:
            crawler[k] = int(body[k])
    for k in policy_float_keys:
        if k in body:
            crawler[k] = float(body[k])

    if "email_enabled" in body:
        extraction.setdefault("email", {})["enabled"] = bool(body["email_enabled"])
    if "email_context_chars" in body:
        extraction.setdefault("email", {})["context_chars"] = int(body["email_context_chars"])
    if "person_enabled" in body:
        extraction.setdefault("person", {})["enabled"] = bool(body["person_enabled"])
    if "person_proximity_chars" in body:
        extraction.setdefault("person", {})["proximity_chars"] = int(body["person_proximity_chars"])

    weight_fields = {
        "weight_email_high": "email_high",
        "weight_email_low": "email_low",
        "weight_person_name": "person_name",
        "weight_designation": "designation",
        "weight_phone": "phone",
    }
    for body_key, weight_key in weight_fields.items():
        if body_key in body:
            weights[weight_key] = int(body[body_key])

    def parse_list(s: str) -> list[str]:
        return [x.strip() for x in s.replace(",", "\n").split("\n") if x.strip()]

    if "target_suffixes" in body:
        crawler["target_suffixes"] = parse_list(body["target_suffixes"])
    if "priority_keywords" in body:
        crawler["priority_keywords"] = parse_list(body["priority_keywords"])
    if "skip_extensions" in body:
        crawler["skip_extensions"] = parse_list(body["skip_extensions"])
    if "valid_suffixes" in body:
        extraction.setdefault("email", {})["valid_suffixes"] = parse_list(body["valid_suffixes"])
    if "title_prefixes" in body:
        extraction.setdefault("person", {})["title_prefixes"] = parse_list(body["title_prefixes"])
    if "designation_keywords" in body:
        extraction.setdefault("person", {})["designation_keywords"] = parse_list(body["designation_keywords"])

    max_links = crawler.setdefault("max_links_per_page", {})
    if "max_links_per_page_0" in body:
        max_links["0"] = int(body["max_links_per_page_0"])
    if "max_links_per_page_1" in body:
        max_links["1"] = int(body["max_links_per_page_1"])
    if "max_links_per_page_2" in body:
        max_links["2"] = int(body["max_links_per_page_2"])
    if "max_links_per_page_default" in body:
        max_links["default"] = int(body["max_links_per_page_default"])

    # Pagination (Story #9) — enabled + the two numeric caps are editable;
    # text_signals/param_signals stay app_settings-only (display-only in the UI).
    pagination = crawler.setdefault("pagination", {})
    if "pagination_enabled" in body:
        pagination["enabled"] = bool(body["pagination_enabled"])
    if "pagination_max_pages" in body:
        pagination["max_pagination_pages"] = int(body["pagination_max_pages"])
    if "pagination_max_chain_children" in body:
        pagination["max_chain_children"] = int(body["pagination_max_chain_children"])

    # DB write first: it's transactional (all-or-nothing). Only once it has
    # durably succeeded do we touch the yaml file, and that write itself is
    # atomic (temp file + os.replace) — so a crash mid-write can never leave
    # config.yaml half-written/corrupt, and a failure here never leaves the
    # two config backends disagreeing about which write "took".
    db.set_app_setting("crawl_policy", policy, updated_by=user.id)

    tmp_path = config_path.with_name(config_path.name + ".tmp")
    with open(tmp_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.replace(tmp_path, config_path)
    c.update(cfg)

    db.write_audit(user.id, "settings.update", detail={"fields": sorted(body.keys())}, ip=client_ip(request))

    if weights != old_weights:
        # Runs off the request/event loop (Starlette offloads sync background
        # tasks to a thread pool) — replaces the old every-startup blanket
        # recompute with one that only fires when weights actually changed.
        background_tasks.add_task(db.recompute_lead_scores)

    return {"message": "Settings saved. Crawler settings take effect on the next job."}
