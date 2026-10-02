"""Zoho Mail OAuth 2.0 routes for inbox connections."""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import Inbox, OAuthState, ZohoAccount
from app.settings_manager import settings
from app.zoho_mail import (
    ZOHO_SCOPES,
    build_authorize_url,
    exchange_code,
    get_accounts,
)

log = logging.getLogger("quickly.zoho_oauth")

router = APIRouter(tags=["zoho-oauth"])
callback_router = APIRouter(tags=["zoho-oauth"])


@router.get("/api/zoho/status")
async def zoho_status(db: AsyncSession = Depends(get_db)):
    configured = bool(settings.zoho_client_id and settings.zoho_client_secret)
    return {
        "configured": configured,
        "redirect_uri": settings.zoho_redirect_uri,
        "scopes": ZOHO_SCOPES,
    }


@router.get("/oauth/zoho/authorize")
async def zoho_authorize(
    display_name: str = "",
    max_per_day: int = 50,
    wait_minutes_between: int = 5,
    max_jitter_seconds: int = 180,
    tracking_domain: str = "",
    ramp_up_enabled: bool = False,
    ramp_up_start: int = 1,
    ramp_up_step_size: int = 1,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    if not settings.zoho_client_id or not settings.zoho_client_secret:
        raise HTTPException(400, "Zoho OAuth is not configured on this Quickly server.")

    csrf = secrets.token_urlsafe(32)
    metadata = {
        "display_name": display_name,
        "max_per_day": max_per_day,
        "wait_minutes_between": wait_minutes_between,
        "max_jitter_seconds": max_jitter_seconds,
        "tracking_domain": tracking_domain or None,
        "ramp_up_enabled": bool(ramp_up_enabled),
        "ramp_up_start": ramp_up_start,
        "ramp_up_step_size": ramp_up_step_size,
    }
    db.add(OAuthState(
        state_token=csrf,
        purpose="inbox_zoho",
        metadata_json=json.dumps(metadata),
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    ))
    await db.flush()

    state = json.dumps({**metadata, "_csrf": csrf})
    url = build_authorize_url(state, settings.zoho_redirect_uri)
    return RedirectResponse(url)


@callback_router.get("/oauth/zoho/callback")
async def zoho_callback(
    request: Request,
    code: str = "",
    error: str = "",
    state: str = "{}",
    db: AsyncSession = Depends(get_db),
):
    if error:
        raise HTTPException(400, f"Zoho OAuth error: {error}")
    if not code:
        raise HTTPException(400, "No authorization code received")

    try:
        state_data = json.loads(state)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(403, "Invalid OAuth state")

    csrf = state_data.get("_csrf", "")
    if not csrf:
        raise HTTPException(403, "Missing OAuth state token")

    row = (await db.execute(
        select(OAuthState).where(OAuthState.state_token == csrf)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(403, "Invalid or expired OAuth state")
    if not hmac.compare_digest(row.purpose, "inbox_zoho"):
        await db.delete(row)
        await db.flush()
        raise HTTPException(403, "Invalid OAuth state purpose")
    if row.expires_at < datetime.utcnow():
        await db.delete(row)
        await db.flush()
        raise HTTPException(403, "OAuth state expired")
    await db.delete(row)
    await db.flush()

    try:
        token_data = exchange_code(code, settings.zoho_redirect_uri)
    except Exception as exc:
        log.exception("Zoho token exchange failed")
        raise HTTPException(502, f"Zoho token exchange failed: {exc}") from exc

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token or not refresh_token:
        raise HTTPException(400, "Zoho did not return an offline refresh token. Revoke Quickly in Zoho and reconnect.")

    expires_in = int(token_data.get("expires_in", 3600))
    token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)

    # Use the OAuth token to discover the user's Zoho Mail account(s).
    temp = ZohoAccount(
        inbox_id=0,
        zoho_email="",
        zoho_account_id="",
        access_token=access_token,
        refresh_token=refresh_token,
        token_expiry=token_expiry,
        scopes=token_data.get("scope", "") or ZOHO_SCOPES,
    )
    try:
        accounts = get_accounts(temp)
    except Exception as exc:
        log.exception("Zoho account discovery failed")
        raise HTTPException(502, f"Could not read Zoho Mail account: {exc}") from exc

    if not accounts:
        raise HTTPException(400, "Zoho OAuth succeeded, but no Zoho Mail account was returned.")

    # Prefer an account with a usable email address.
    selected = next(
        (
            a for a in accounts
            if (a.get("emailAddress") or a.get("email") or a.get("mailId"))
        ),
        accounts[0],
    )
    email = (
        selected.get("emailAddress")
        or selected.get("email")
        or selected.get("mailId")
        or ""
    ).strip().lower()
    account_id = str(
        selected.get("accountId")
        or selected.get("accountid")
        or selected.get("id")
        or ""
    )
    if not email or not account_id:
        raise HTTPException(502, "Zoho returned an account without email/account ID.")

    # Reuse an existing inbox if the address is already connected.
    inbox = (await db.execute(
        select(Inbox).where(Inbox.email == email)
    )).scalar_one_or_none()

    if inbox:
        inbox.provider = "zoho"
        if state_data.get("display_name"):
            inbox.display_name = state_data["display_name"]
        account = (await db.execute(
            select(ZohoAccount).where(ZohoAccount.inbox_id == inbox.id)
        )).scalar_one_or_none()
        if account is None:
            account = ZohoAccount(inbox_id=inbox.id)
            db.add(account)
    else:
        inbox = Inbox(
            email=email,
            display_name=state_data.get("display_name") or email.split("@")[0],
            max_emails_per_day=int(state_data.get("max_per_day", 50)),
            wait_minutes_between=int(state_data.get("wait_minutes_between", 5)),
            max_jitter_seconds=int(state_data.get("max_jitter_seconds", 180)),
            provider="zoho",
            tracking_domain=state_data.get("tracking_domain") or None,
            ramp_up_enabled=bool(state_data.get("ramp_up_enabled", False)),
            ramp_up_start=int(state_data.get("ramp_up_start", 1)),
            ramp_up_step_size=int(state_data.get("ramp_up_step_size", 1)),
            ramp_up_started_at=datetime.utcnow() if state_data.get("ramp_up_enabled") else None,
        )
        db.add(inbox)
        await db.flush()
        account = ZohoAccount(inbox_id=inbox.id)
        db.add(account)

    account.zoho_email = email
    account.zoho_account_id = account_id
    account.access_token = access_token
    account.refresh_token = refresh_token
    account.token_expiry = token_expiry
    account.scopes = token_data.get("scope", "") or ZOHO_SCOPES
    account.updated_at = datetime.utcnow()
    await db.flush()

    log.info("Zoho Mail OAuth connected: %s (inbox_id=%s)", email, inbox.id)

    base = settings.base_url.rstrip("/")
    target = f"{base}/inboxes?connected={urllib.parse.quote(email)}"
    return RedirectResponse(target, status_code=303)


@router.delete("/api/zoho/accounts/{account_id}")
async def disconnect_zoho(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    account = (await db.execute(
        select(ZohoAccount).where(ZohoAccount.id == account_id)
    )).scalar_one_or_none()
    if not account:
        raise HTTPException(404, "Zoho account not found")
    inbox = (await db.execute(select(Inbox).where(Inbox.id == account.inbox_id))).scalar_one_or_none()
    email = account.zoho_email
    await db.delete(account)
    if inbox and inbox.provider == "zoho":
        inbox.provider = "resend"
    await db.flush()
    return {"ok": True, "email": email}
