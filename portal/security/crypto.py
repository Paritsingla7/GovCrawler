"""SMTP credential encryption at rest (Fernet/cryptography).

Same env-first-else-persisted-file pattern as server.py's _ensure_jwt_secret:
containers supply CREDENTIAL_ENC_KEY via env; local/dev installs get one
generated and persisted to config.yaml on first run. Losing this key makes
every stored credential permanently undecryptable — there is no recovery
path, by design (plan.md §13).
"""
import logging
import os

import yaml
from cryptography.fernet import Fernet, InvalidToken
from pathlib import Path

log = logging.getLogger(__name__)


def ensure_credential_enc_key(config_dict: dict, config_path: Path) -> str:
    if os.environ.get("CREDENTIAL_ENC_KEY"):
        key = os.environ["CREDENTIAL_ENC_KEY"]
        config_dict.setdefault("auth", {})["credential_enc_key"] = key
        return key
    existing = config_dict.get("auth", {}).get("credential_enc_key")
    if existing:
        return existing
    key = Fernet.generate_key().decode()
    config_dict.setdefault("auth", {})["credential_enc_key"] = key
    with open(config_path, "w") as f:
        yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    log.info("Generated and persisted a new auth.credential_enc_key")
    return key


def encrypt_password(plain: str, key: str) -> bytes:
    return Fernet(key.encode()).encrypt(plain.encode())


def decrypt_password(blob, key: str) -> str:
    if isinstance(blob, str):
        blob = blob.encode()
    try:
        return Fernet(key.encode()).decrypt(bytes(blob)).decode()
    except InvalidToken:
        raise ValueError("Could not decrypt credential — CREDENTIAL_ENC_KEY may have changed")
