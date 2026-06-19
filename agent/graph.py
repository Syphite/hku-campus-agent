import os
import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
USER_EMAIL = os.getenv("GRAPH_USER_ID", "hku.demo.agent@outlook.com")

# Protected senders — never archive regardless of content
PROTECTED_SENDERS = [
    "registry.hku.hk", "aaoffice.hku.hk", "cedars.hku.hk",
    "financial-aid.hku.hk", "scholarships@hku.hk", "aaso@hku.hk"
]


def get_access_token() -> str:
    tenant_id     = os.getenv("GRAPH_TENANT_ID")
    client_id     = os.getenv("GRAPH_CLIENT_ID")
    client_secret = os.getenv("GRAPH_CLIENT_SECRET")

    if not all([tenant_id, client_id, client_secret]):
        raise RuntimeError("Missing GRAPH_TENANT_ID, GRAPH_CLIENT_ID, or GRAPH_CLIENT_SECRET")

    url  = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "https://graph.microsoft.com/.default"
    }
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Could not obtain access token")
    return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}",
            "Content-Type": "application/json"}


def get_unread_emails(user_email: str = USER_EMAIL) -> list:
    url    = f"{GRAPH_BASE}/users/{user_email}/mailFolders/inbox/messages"
    params = {
        "$filter": "isRead eq false",
        "$top":    25,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId"
    }
    resp = requests.get(url, headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_or_create_archive_folder(user_email: str = USER_EMAIL) -> str:
    """Get or create the Agent Archived folder. Returns folder ID."""
    url  = f"{GRAPH_BASE}/users/{user_email}/mailFolders"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    for folder in resp.json().get("value", []):
        if folder.get("displayName") == "Agent Archived":
            return folder["id"]
    # Create it
    resp = requests.post(url, headers=_headers(),
                         json={"displayName": "Agent Archived"}, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def archive_email(email_id: str, user_email: str = USER_EMAIL) -> dict:
    """Move email to Agent Archived folder. Never deletes."""
    folder_id = get_or_create_archive_folder(user_email)
    url  = f"{GRAPH_BASE}/users/{user_email}/messages/{email_id}/move"
    resp = requests.post(url, headers=_headers(),
                         json={"destinationId": folder_id}, timeout=30)
    if resp.status_code not in (200, 201):
        return {"success": False, "new_id": ""}
    return {"success": True, "new_id": resp.json().get("id", "")}


def restore_email(email_id: str, user_email: str = USER_EMAIL) -> bool:
    """Move email back to inbox (undo archive)."""
    url  = f"{GRAPH_BASE}/users/{user_email}/messages/{email_id}/move"
    resp = requests.post(url, headers=_headers(),
                         json={"destinationId": "inbox"}, timeout=30)
    return resp.status_code in (200, 201)


def is_protected_sender(email_address: str) -> bool:
    """Check if sender should never be archived."""
    addr = email_address.lower()
    return any(p in addr for p in PROTECTED_SENDERS)
