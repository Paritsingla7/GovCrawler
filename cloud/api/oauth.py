"""OAuth2 callback for SMTP credentials (Microsoft/Google). Mounted bare, with
no auth dependencies — like auth.router — since the provider's redirect back
here carries no session cookie or bearer token, only a `code`/`state` pair
that ties back to the credential via cloud/db's oauth_pending_flows table.
See .docs/outreach.md and cloud/api/credentials.py's /oauth/start."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .deps import client_ip, get_db
from ..db import Database
from ..security.oauth import OAuthTokenError, exchange_code

log = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{title}</title></head>"
        f"<body style='font-family: sans-serif; max-width: 32rem; margin: 4rem auto; text-align: center;'>"
        f"<h2>{title}</h2><p>{body}</p></body></html>",
        status_code=status_code,
    )


@router.get("/oauth/callback/{provider}")
async def oauth_callback(
    provider: str,
    request: Request,
    db: Database = Depends(get_db),
):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error_description") or request.query_params.get("error")

    if error:
        return _page("Connection failed", f"{provider.title()} declined the request: {error}", status_code=400)
    if not code or not state:
        return _page("Connection failed", "Missing authorization code or state.", status_code=400)

    flow = db.consume_oauth_flow(state)
    if not flow or flow["provider"] != provider:
        return _page(
            "Connection failed",
            "This sign-in link has expired or was already used — go back to Settings and click Connect again.",
            status_code=400,
        )

    oauth_config = db.get_oauth_config()
    redirect_base = oauth_config.get("redirect_base_url", "").rstrip("/")
    redirect_uri = f"{redirect_base}/oauth/callback/{provider}"

    try:
        tokens = await exchange_code(provider, oauth_config, redirect_uri, code, flow["code_verifier"])
    except OAuthTokenError as e:
        log.warning(f"OAuth callback: {provider} token exchange failed for credential {flow['credential_id']}: {e}")
        return _page("Connection failed", str(e), status_code=400)

    if not tokens["refresh_token"]:
        log.error(f"OAuth callback: {provider} returned no refresh_token for credential {flow['credential_id']}")
        return _page(
            "Connection failed",
            f"{provider.title()} didn't return a long-lived refresh token — try disconnecting this app's "
            "access in your account settings, then Connect again.",
            status_code=400,
        )

    db.update_credential_tokens(
        flow["credential_id"],
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=tokens["expires_at"],
    )
    db.update_credential(flow["credential_id"], is_active=True)
    db.write_audit(
        None,
        "credential.oauth_connected",
        "credential",
        flow["credential_id"],
        detail={"provider": provider},
        ip=client_ip(request),
    )
    return _page("Connected", "You can close this tab and return to Settings.")
