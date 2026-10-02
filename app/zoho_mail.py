"""Zoho Mail OAuth/API helpers for Quickly."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from app.models import ZohoAccount
from app.settings_manager import settings

log = logging.getLogger("quickly.zoho")

ZOHO_AUTH_URL = "https://accounts.zoho.in/oauth/v2/auth"
ZOHO_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
ZOHO_MAIL_ROOT = "https://mail.zoho.in/api"

ZOHO_SCOPES = (
    "ZohoMail.accounts.READ "
    "ZohoMail.messages.CREATE "
    "ZohoMail.messages.READ"
)


class ZohoAPIError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"Zoho Mail API error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def build_authorize_url(state: str, redirect_uri: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.zoho_client_id,
        "scope": ZOHO_SCOPES,
        "redirect_uri": redirect_uri,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{ZOHO_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    return _token_request({
        "code": code,
        "client_id": settings.zoho_client_id,
        "client_secret": settings.zoho_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })


def refresh_access_token(account: ZohoAccount) -> str | None:
    if not account.refresh_token:
        return None
    try:
        data = _token_request({
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
        })
    except Exception as exc:
        log.error("Zoho token refresh failed for %s: %s", account.zoho_email, exc)
        return None

    token = data.get("access_token")
    if not token:
        return None
    account.access_token = token
    account.token_expiry = datetime.utcnow() + timedelta(
        seconds=int(data.get("expires_in", 3600))
    )
    account.updated_at = datetime.utcnow()
    return token


def ensure_access_token(account: ZohoAccount) -> str | None:
    if account.access_token and account.token_expiry:
        if (account.token_expiry - datetime.utcnow()).total_seconds() > 120:
            return account.access_token
    elif account.access_token:
        return account.access_token
    return refresh_access_token(account)


def _token_request(params: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        ZOHO_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise ZohoAPIError(exc.code, body_text) from exc
    except Exception as exc:
        raise ZohoAPIError(0, str(exc)) from exc

    if data.get("error"):
        raise ZohoAPIError(400, json.dumps(data))
    return data


def request(
    method: str,
    path: str,
    access_token: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{ZOHO_MAIL_ROOT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise ZohoAPIError(exc.code, body_text) from exc
    except Exception as exc:
        raise ZohoAPIError(0, str(exc)) from exc


def get_accounts(account: ZohoAccount) -> list[dict[str, Any]]:
    token = ensure_access_token(account)
    if not token:
        raise ZohoAPIError(401, "No Zoho access token")
    data = request("GET", "/accounts", token)
    raw = data.get("data", data)
    if isinstance(raw, dict):
        raw = raw.get("accounts", raw.get("data", []))
    return raw if isinstance(raw, list) else []


def send_message(
    account: ZohoAccount,
    *,
    to_email: str,
    subject: str,
    content: str,
    from_email: str,
    from_name: str = "",
    is_html: bool = False,
) -> dict[str, Any]:
    token = ensure_access_token(account)
    if not token:
        raise ZohoAPIError(401, "No Zoho access token")

    payload: dict[str, Any] = {
        "fromAddress": f"{from_name} <{from_email}>" if from_name else from_email,
        "toAddress": to_email,
        "subject": subject,
        "content": content,
        "mailFormat": "html" if is_html else "plaintext",
    }
    return request(
        "POST",
        f"/accounts/{account.zoho_account_id}/messages",
        token,
        payload=payload,
    )
