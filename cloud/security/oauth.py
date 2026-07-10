"""OAuth2 (Authorization Code + PKCE) helpers for Microsoft/Google SMTP credentials.

SMTP AUTH stays exactly what it was (aiosmtplib) — this module only resolves a
valid access token; the send path calls `smtp.auth_xoauth2()` (native to
aiosmtplib>=5.1.0) instead of `smtp.login()`. See .docs/outreach.md.

One consent per mailbox, no tenant-admin consent required: Microsoft uses the
multi-tenant + personal-account "common" endpoint since outreach mailboxes
span more than one organization.
"""

import datetime
import logging
import secrets

from authlib.common.security import generate_token
from authlib.integrations.httpx_client import AsyncOAuth2Client

log = logging.getLogger(__name__)


class OAuthTokenError(Exception):
    """A code exchange or refresh-token grant failed (denied/revoked consent,
    expired refresh token, misconfigured client). Caught alongside
    aiosmtplib.SMTPAuthenticationError wherever a credential is used, so a
    revoked consent disables the credential exactly like a wrong password does."""


_PROVIDERS = {
    "microsoft": {
        "authorize_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "https://outlook.office365.com/SMTP.Send offline_access User.Read",
        "token_endpoint_auth_method": "none",  # public client, no client_secret
        "extra_authorize_params": {},
    },
    "google": {
        "authorize_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "scope": "https://mail.google.com/",
        "token_endpoint_auth_method": "client_secret_post",
        # access_type=offline is required to get a refresh_token back at all;
        # prompt=consent forces one even on a re-connect where Google would
        # otherwise silently skip issuing a new one.
        "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
    },
}

_REFRESH_LEEWAY_SECONDS = 60


def _provider_config(provider: str) -> dict:
    try:
        return _PROVIDERS[provider]
    except KeyError:
        raise ValueError(f"Unknown OAuth provider: {provider!r}")


def _client_credentials(provider: str, oauth_config: dict) -> tuple[str, str | None]:
    section = (oauth_config or {}).get(provider, {})
    client_id = section.get("client_id")
    if not client_id:
        raise OAuthTokenError(f"oauth.{provider}.client_id is not configured")
    return client_id, section.get("client_secret")


def _build_client(provider: str, oauth_config: dict, redirect_uri: str | None = None) -> AsyncOAuth2Client:
    cfg = _provider_config(provider)
    client_id, client_secret = _client_credentials(provider, oauth_config)
    return AsyncOAuth2Client(
        client_id=client_id,
        client_secret=client_secret,
        scope=cfg["scope"],
        redirect_uri=redirect_uri,
        code_challenge_method="S256",
        token_endpoint_auth_method=cfg["token_endpoint_auth_method"],
    )


def build_authorize_url(provider: str, oauth_config: dict, redirect_uri: str) -> tuple[str, str, str]:
    """Returns (authorize_url, state, code_verifier). Caller persists
    state -> (credential_id, code_verifier) until the callback consumes it."""
    cfg = _provider_config(provider)
    client = _build_client(provider, oauth_config, redirect_uri)
    state = secrets.token_urlsafe(32)
    code_verifier = generate_token(64)
    authorize_url, state = client.create_authorization_url(
        cfg["authorize_endpoint"],
        state=state,
        code_verifier=code_verifier,
        **cfg["extra_authorize_params"],
    )
    return authorize_url, state, code_verifier


def _normalize_token(token: dict) -> dict:
    expires_at = token.get("expires_at")
    return {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": datetime.datetime.utcfromtimestamp(expires_at) if expires_at else None,
    }


async def exchange_code(provider: str, oauth_config: dict, redirect_uri: str, code: str, code_verifier: str) -> dict:
    """Authorization-code + PKCE token exchange. Returns
    {access_token, refresh_token, expires_at}."""
    cfg = _provider_config(provider)
    client = _build_client(provider, oauth_config, redirect_uri)
    try:
        async with client:
            token = await client.fetch_token(
                cfg["token_endpoint"],
                grant_type="authorization_code",
                code=code,
                code_verifier=code_verifier,
            )
    except OAuthTokenError:
        raise
    except Exception as e:
        raise OAuthTokenError(f"{provider} token exchange failed: {e}") from e
    return _normalize_token(token)


async def refresh_access_token(provider: str, oauth_config: dict, refresh_token: str) -> dict:
    """Refresh-token grant. Providers may reissue a new refresh_token —
    callers must persist whatever comes back, not assume the old one lives on."""
    cfg = _provider_config(provider)
    client = _build_client(provider, oauth_config)
    try:
        async with client:
            token = await client.fetch_token(
                cfg["token_endpoint"],
                grant_type="refresh_token",
                refresh_token=refresh_token,
            )
    except OAuthTokenError:
        raise
    except Exception as e:
        raise OAuthTokenError(f"{provider} token refresh failed: {e}") from e
    return _normalize_token(token)


async def get_valid_access_token(db, credential: dict, oauth_config: dict) -> str:
    """A live access token for a non-'basic' credential, refreshing it first if
    expired/near-expiry. Persists a refreshed pair via db.update_credential_tokens
    (a narrow, unaudited update — this runs on every send, not just user edits)."""
    expires_at = credential.get("token_expires_at")
    access_token = credential.get("access_token")
    now = datetime.datetime.utcnow()
    if access_token and expires_at and expires_at > now + datetime.timedelta(seconds=_REFRESH_LEEWAY_SECONDS):
        return access_token

    refresh_token = credential.get("refresh_token")
    if not refresh_token:
        raise OAuthTokenError("Credential has no refresh token — connect it via OAuth first")

    fresh = await refresh_access_token(credential["provider"], oauth_config, refresh_token)
    db.update_credential_tokens(
        credential["id"],
        access_token=fresh["access_token"],
        refresh_token=fresh["refresh_token"] or refresh_token,
        expires_at=fresh["expires_at"],
    )
    return fresh["access_token"]
