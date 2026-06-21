"""
agent/graph_diagnostics.py

Staged Microsoft Graph probes and plain-English hints for auth/mailbox failures.
Used by scripts/diagnose_graph.py and runtime logging in graph.py.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MAIL_READ_ROLES = {"Mail.Read", "Mail.ReadWrite", "Mail.ReadBasic", "Mail.ReadBasic.All"}


def _redact_secret(value: str | None) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verifying signature (diagnostic only)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padding = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + padding)
        return json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def get_config_report() -> dict:
    """Report which Graph env vars are set (secrets redacted)."""
    return {
        "GRAPH_TENANT_ID": os.getenv("GRAPH_TENANT_ID") or "(not set)",
        "GRAPH_CLIENT_ID": _redact_secret(os.getenv("GRAPH_CLIENT_ID")),
        "GRAPH_CLIENT_SECRET": _redact_secret(os.getenv("GRAPH_CLIENT_SECRET")),
        "GRAPH_USER_ID": os.getenv("GRAPH_USER_ID") or "(not set)",
    }


def fetch_access_token_report() -> dict:
    """
    Acquire app-only token and decode roles/expiry.
    Does not import agent.graph to avoid circular imports.
    """
    tenant_id = os.getenv("GRAPH_TENANT_ID")
    client_id = os.getenv("GRAPH_CLIENT_ID")
    client_secret = os.getenv("GRAPH_CLIENT_SECRET")

    missing = [
        name for name, value in (
            ("GRAPH_TENANT_ID", tenant_id),
            ("GRAPH_CLIENT_ID", client_id),
            ("GRAPH_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        return {
            "ok": False,
            "error": f"Missing env vars: {', '.join(missing)}",
            "roles": [],
        }

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
    }
    try:
        resp = requests.post(url, data=data, timeout=30)
        if resp.status_code != 200:
            body = resp.text[:500]
            return {
                "ok": False,
                "error": f"Token endpoint returned {resp.status_code}: {body}",
                "roles": [],
                "status": resp.status_code,
            }
        token = resp.json().get("access_token")
        if not token:
            return {"ok": False, "error": "Token response missing access_token", "roles": []}

        payload = decode_jwt_payload(token)
        roles = payload.get("roles") or []
        exp = payload.get("exp")
        expires_at = ""
        if exp:
            try:
                expires_at = datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                expires_at = str(exp)

        return {
            "ok": True,
            "token": token,
            "app_id": payload.get("appid") or payload.get("azp") or "",
            "tenant_id": payload.get("tid") or tenant_id,
            "roles": list(roles) if isinstance(roles, list) else [],
            "expires_at": expires_at,
            "error": "",
        }
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "roles": []}


def parse_graph_error_response(resp: requests.Response) -> dict:
    """Extract structured error fields from a Graph HTTP response."""
    result = {
        "status": resp.status_code,
        "ok": resp.ok,
        "error_code": "",
        "error_message": "",
        "inner_error": {},
        "request_id": resp.headers.get("request-id") or resp.headers.get("client-request-id") or "",
        "www_authenticate": resp.headers.get("WWW-Authenticate") or "",
        "body": resp.text[:1000] if resp.text else "",
    }
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            err = payload.get("error") or {}
            if isinstance(err, dict):
                result["error_code"] = str(err.get("code") or "")
                result["error_message"] = str(err.get("message") or "")
                inner = err.get("innerError")
                if isinstance(inner, dict):
                    result["inner_error"] = inner
                    if not result["request_id"]:
                        result["request_id"] = str(inner.get("request-id") or "")
    except ValueError:
        pass
    return result


def graph_probe(
    method: str,
    url: str,
    token: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: int = 30,
) -> dict:
    """Call Graph and return structured result without raising."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.request(
            method.upper(),
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )
        parsed = parse_graph_error_response(resp)
        parsed["url"] = url
        parsed["method"] = method.upper()
        return parsed
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": 0,
            "method": method.upper(),
            "url": url,
            "error_code": "RequestException",
            "error_message": str(exc),
            "inner_error": {},
            "request_id": "",
            "www_authenticate": "",
            "body": "",
        }


def _user_url(user_id: str) -> str:
    return f"{GRAPH_BASE}/users/{quote(user_id, safe='@')}"


def diagnose_mailbox_access(user_id: str, token: str | None = None) -> dict:
    """Run staged probes for a specific Graph user identifier."""
    token_report = fetch_access_token_report()
    if not token_report.get("ok"):
        return {
            "config": get_config_report(),
            "token": token_report,
            "user_lookup": None,
            "inbox_read": None,
            "hints": [token_report.get("error") or "Could not acquire access token."],
            "ok": False,
        }

    access_token = token or token_report["token"]
    user_lookup = graph_probe("GET", _user_url(user_id), access_token, params={"$select": "id,displayName,userPrincipalName,mail,userType"})
    inbox_url = f"{GRAPH_BASE}/users/{quote(user_id, safe='@')}/mailFolders/inbox/messages"
    inbox_read = graph_probe(
        "GET",
        inbox_url,
        access_token,
        params={"$top": 1, "$select": "id,subject"},
    )

    report = {
        "config": get_config_report(),
        "token": {
            "ok": True,
            "app_id": token_report.get("app_id"),
            "tenant_id": token_report.get("tenant_id"),
            "roles": token_report.get("roles", []),
            "expires_at": token_report.get("expires_at"),
        },
        "user_id": user_id,
        "user_lookup": user_lookup,
        "inbox_read": inbox_read,
        "ok": bool(user_lookup.get("ok") and inbox_read.get("ok")),
    }
    report["hints"] = interpret_graph_failure(report)
    return report


def interpret_graph_failure(report: dict) -> list[str]:
    """Map probe results to actionable hints."""
    hints: list[str] = []
    token = report.get("token") or {}
    roles = set(token.get("roles") or [])
    user_lookup = report.get("user_lookup") or {}
    inbox_read = report.get("inbox_read") or {}

    if not token.get("ok", True):
        hints.append(str(report.get("token", {}).get("error") or "Access token could not be acquired."))
        return hints

    if roles and not (roles & MAIL_READ_ROLES):
        hints.append(
            "App token is valid but missing Mail.Read / Mail.ReadWrite application permission "
            "(or admin consent). Add Mail.Read application permission in Entra and grant admin consent."
        )
    elif not roles:
        hints.append(
            "Token has no application roles in JWT. Confirm app registration uses application "
            "permissions (not delegated-only) and admin consent was granted."
        )

    if user_lookup.get("status") == 404:
        hints.append(
            "User not found in tenant. Set GRAPH_USER_ID to the Entra Object ID or full UPN "
            "(e.g. guest #EXT# UPN), not a personal Gmail login address."
        )
    elif user_lookup.get("status") == 401:
        hints.append(
            "User lookup returned 401 — token may be invalid for this tenant or app lacks User.Read.All."
        )
    elif not user_lookup.get("ok") and user_lookup.get("status"):
        hints.append(
            f"User lookup failed ({user_lookup.get('status')}): "
            f"{user_lookup.get('error_code') or user_lookup.get('error_message') or 'unknown error'}"
        )

    if user_lookup.get("ok") and not inbox_read.get("ok"):
        code = inbox_read.get("error_code") or ""
        status = inbox_read.get("status")
        if status == 401 or "ErrorAccessDenied" in code or "Authorization" in code:
            hints.append(
                "Inbox read denied with app-only auth. Personal/consumer Outlook mailboxes "
                "(including Gmail-linked accounts) often cannot be accessed via client credentials. "
                "Use an org mailbox in the same tenant, or switch to delegated OAuth for the signed-in user."
            )
        elif status == 403:
            hints.append(
                "Inbox returned 403 Forbidden — check Mail.Read application permission, "
                "admin consent, and Exchange application access policies."
            )
        else:
            hints.append(
                f"Inbox read failed ({status}): "
                f"{inbox_read.get('error_code') or inbox_read.get('error_message') or inbox_read.get('body', '')[:200]}"
            )

    if inbox_read.get("www_authenticate"):
        hints.append(f"WWW-Authenticate: {inbox_read['www_authenticate'][:300]}")

    if not hints:
        if report.get("ok"):
            hints.append("All probes succeeded — Graph mailbox access looks healthy.")
        else:
            hints.append("Graph access failed; review status codes and error messages above.")

    return hints


def compare_mailbox_identifiers(primary_user_id: str, profile_email: str | None) -> dict[str, Any]:
    """Compare inbox access for GRAPH_USER_ID vs profile email fallback."""
    primary = diagnose_mailbox_access(primary_user_id)
    comparison = {"primary": primary, "profile_email": None}
    if profile_email and profile_email != primary_user_id:
        comparison["profile_email"] = diagnose_mailbox_access(profile_email)
        if primary.get("ok") and not comparison["profile_email"].get("ok"):
            comparison["mismatch_hint"] = (
                "Inbox works with GRAPH_USER_ID but fails with profile email. "
                "Ensure email_pipeline uses resolve_user_email(profile.get('email'))."
            )
        elif not primary.get("ok") and comparison["profile_email"].get("ok"):
            comparison["mismatch_hint"] = (
                "Profile email works but GRAPH_USER_ID does not — update GRAPH_USER_ID."
            )
    return comparison


def format_probe_line(probe: dict | None, label: str) -> str:
    if not probe:
        return f"{label}: (skipped)"
    status = probe.get("status", "?")
    ok = probe.get("ok")
    parts = [f"{label}: status={status} ok={ok}"]
    if probe.get("error_code"):
        parts.append(f"code={probe['error_code']}")
    if probe.get("error_message"):
        parts.append(f"message={probe['error_message'][:200]}")
    if probe.get("request_id"):
        parts.append(f"request_id={probe['request_id']}")
    return " | ".join(parts)
