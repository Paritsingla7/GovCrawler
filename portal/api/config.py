"""
Crawler/extraction configuration endpoints.

Registers routes:
  GET  /api/config    → current crawler settings
  POST /api/config    → save crawler settings
"""

import copy

import yaml
from fastapi import APIRouter, Depends

from .deps import get_config as get_app_config, get_config_path

router = APIRouter(tags=["config"])


@router.get("/api/config")
async def get_config(c: dict = Depends(get_app_config)):
    return {
        "workers": c["crawler"]["workers"],
        "max_depth": c["crawler"]["max_depth"],
        "recrawl_days": c["crawler"]["recrawl_days"],
        "request_delay": c["crawler"]["request_delay"],
        "per_url_timeout": c["crawler"]["per_url_timeout"],
        "httpx_first": c["crawler"].get("httpx_first", True),
        "playwright_fallback": c["crawler"].get("playwright_fallback", True),
        "playwright_timeout": c["crawler"]["playwright_timeout"],
        "js_settle_time": c["crawler"]["js_settle_time"],
        "email_enabled": c["extraction"]["email"]["enabled"],
        "email_context_chars": c["extraction"]["email"]["context_chars"],
        "person_enabled": c["extraction"]["person"]["enabled"],
        "person_proximity_chars": c["extraction"]["person"]["proximity_chars"],

        # Arrays
        "target_suffixes": "\n".join(c["crawler"].get("target_suffixes", [])),
        "priority_keywords": "\n".join(c["crawler"].get("priority_keywords", [])),
        "skip_extensions": "\n".join(c["crawler"].get("skip_extensions", [])),
        "valid_suffixes": "\n".join(c["extraction"]["email"].get("valid_suffixes", [])),
        "title_prefixes": "\n".join(c["extraction"]["person"].get("title_prefixes", [])),
        "designation_keywords": "\n".join(c["extraction"]["person"].get("designation_keywords", [])),

        # Dictionary
        "max_links_per_page_0": c["crawler"].get("max_links_per_page", {}).get(0, 30),
        "max_links_per_page_1": c["crawler"].get("max_links_per_page", {}).get(1, 15),
        "max_links_per_page_2": c["crawler"].get("max_links_per_page", {}).get(2, 8),
        "max_links_per_page_default": c["crawler"].get("max_links_per_page", {}).get("default", 5),

        # Read-only
        "user_agent": c["crawler"].get("user_agent", ""),
        "js_indicators": "\n".join(c["crawler"].get("js_indicators", [])),
        "email_regex": c["extraction"]["email"].get("regex", ""),
        "email_obfuscation": yaml.dump(c["extraction"]["email"].get("obfuscation", []), default_flow_style=False),

        # Pagination (Story #9) — enabled + the two numeric caps are editable
        # via POST /api/config below. text_signals/param_signals stay
        # display-only here; edit config.yaml directly to change those lists.
        "pagination_enabled": c["crawler"].get("pagination", {}).get("enabled", False),
        "pagination_max_pages": c["crawler"].get("pagination", {}).get("max_pagination_pages", 50),
        "pagination_max_chain_children": c["crawler"].get("pagination", {}).get("max_chain_children", 100),
        "pagination_text_signals": "\n".join(c["crawler"].get("pagination", {}).get("text_signals", [])),
        "pagination_param_signals": "\n".join(c["crawler"].get("pagination", {}).get("param_signals", [])),
    }


@router.post("/api/config")
async def save_config(body: dict, c: dict = Depends(get_app_config), config_path=Depends(get_config_path)):
    cfg = copy.deepcopy(c)

    int_keys = {"workers", "max_depth", "recrawl_days", "per_url_timeout", "playwright_timeout"}
    float_keys = {"request_delay", "js_settle_time"}
    bool_keys = {"httpx_first", "playwright_fallback"}

    for k in int_keys:
        if k in body:
            cfg["crawler"][k] = int(body[k])
    for k in float_keys:
        if k in body:
            cfg["crawler"][k] = float(body[k])
    for k in bool_keys:
        if k in body:
            cfg["crawler"][k] = bool(body[k])

    if "email_enabled" in body:
        cfg["extraction"]["email"]["enabled"] = bool(body["email_enabled"])
    if "email_context_chars" in body:
        cfg["extraction"]["email"]["context_chars"] = int(body["email_context_chars"])
    if "person_enabled" in body:
        cfg["extraction"]["person"]["enabled"] = bool(body["person_enabled"])
    if "person_proximity_chars" in body:
        cfg["extraction"]["person"]["proximity_chars"] = int(body["person_proximity_chars"])

    def parse_list(s: str) -> list[str]:
        return [x.strip() for x in s.replace(",", "\n").split("\n") if x.strip()]

    if "target_suffixes" in body:
        cfg["crawler"]["target_suffixes"] = parse_list(body["target_suffixes"])
    if "priority_keywords" in body:
        cfg["crawler"]["priority_keywords"] = parse_list(body["priority_keywords"])
    if "skip_extensions" in body:
        cfg["crawler"]["skip_extensions"] = parse_list(body["skip_extensions"])
    if "valid_suffixes" in body:
        cfg["extraction"]["email"]["valid_suffixes"] = parse_list(body["valid_suffixes"])
    if "title_prefixes" in body:
        cfg["extraction"]["person"]["title_prefixes"] = parse_list(body["title_prefixes"])
    if "designation_keywords" in body:
        cfg["extraction"]["person"]["designation_keywords"] = parse_list(body["designation_keywords"])

    # dict updates
    max_links = cfg["crawler"].setdefault("max_links_per_page", {})
    if "max_links_per_page_0" in body:
        max_links[0] = int(body["max_links_per_page_0"])
    if "max_links_per_page_1" in body:
        max_links[1] = int(body["max_links_per_page_1"])
    if "max_links_per_page_2" in body:
        max_links[2] = int(body["max_links_per_page_2"])
    if "max_links_per_page_default" in body:
        max_links["default"] = int(body["max_links_per_page_default"])

    # Pagination (Story #9) — enabled + the two numeric caps are editable;
    # text_signals/param_signals stay config.yaml-only (display-only in the UI).
    pagination = cfg["crawler"].setdefault("pagination", {})
    if "pagination_enabled" in body:
        pagination["enabled"] = bool(body["pagination_enabled"])
    if "pagination_max_pages" in body:
        pagination["max_pagination_pages"] = int(body["pagination_max_pages"])
    if "pagination_max_chain_children" in body:
        pagination["max_chain_children"] = int(body["pagination_max_chain_children"])

    c.update(cfg)

    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return {"message": "Settings saved. Crawler settings take effect on the next job."}
