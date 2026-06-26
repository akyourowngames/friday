"""Google Workspace MCP bridge for Ares.

Uses the OAuth tokens already stored by Ares's MCP client to make direct
REST API calls to Gmail and Calendar via google-api-python-client.
No Developer Preview enrollment needed.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

TOKEN_DIR = Path(os.path.expanduser("~/.ares/data/mcp_tokens"))
CONFIG_PATH = Path(os.path.expanduser("~/.ares/config.json"))


def _get_client_credentials() -> tuple[str, str]:
    """Read OAuth client credentials from Ares config."""
    try:
        servers = json.loads(CONFIG_PATH.read_text()).get("mcp_servers", [])
        for server in servers:
            cid = server.get("oauth_client_id", "")
            csec = server.get("oauth_client_secret", "")
            if cid and csec:
                return cid, csec
    except Exception:
        pass
    # Fallback to env vars
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if cid and csec:
        return cid, csec
    return "", ""

# Cache service objects keyed by (service_name, version, token_name)
_service_cache: dict[str, Any] = {}


def _get_credentials(token_name: str) -> Credentials:
    """Load and return auto-refreshing credentials from a saved token file."""
    token_path = TOKEN_DIR / f"{token_name}.json"
    if not token_path.exists():
        raise FileNotFoundError(
            f"Token file not found at {token_path}. "
            "Run MCP server authentication first."
        )
    data = json.loads(token_path.read_text())
    scopes = data.get("scope", data.get("scopes", ""))
    if isinstance(scopes, str):
        scopes = scopes.split()
    client_id, client_secret = _get_client_credentials()
    creds = Credentials(
        token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id or data.get("client_id", ""),
        client_secret=client_secret or data.get("client_secret", ""),
        scopes=scopes or None,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(AuthRequest())
    return creds


def _get_service(service_name: str, version: str, token_name: str):
    """Get or create a cached Google API service object."""
    key = f"{service_name}:{version}:{token_name}"
    if key not in _service_cache:
        creds = _get_credentials(token_name)
        _service_cache[key] = build(
            service_name, version, credentials=creds, cache_discovery=False
        )
    return _service_cache[key]


def _maybe_refresh_token_on_401(token_name: str) -> None:
    """Force-refresh cached service for token_name on 401 errors."""
    for key in list(_service_cache.keys()):
        if key.endswith(f":{token_name}"):
            del _service_cache[key]


# ── Gmail ──


def gmail_search(query: str = "", max_results: int = 20) -> str:
    """Search Gmail messages.

    Args:
        query: Gmail search syntax, e.g. 'is:unread', 'from:someone@example.com'
        max_results: Number of results to return (max 50)

    Returns:
        JSON string with message list.
    """
    try:
        service = _get_service("gmail", "v1", "gmail")
        max_results = max(1, min(max_results, 50))
        result = (
            service.users()
            .messages()
            .list(userId="me", q=query or "", maxResults=max_results)
            .execute()
        )
        messages = result.get("messages", [])
        if not messages:
            return json.dumps({"count": 0, "messages": []}, indent=2)

        out = []
        for msg in messages[:max_results]:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=msg["id"], format="metadata", metadataHeaders=["Subject", "From", "Date", "To"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
            out.append({
                "id": msg["id"],
                "threadId": full.get("threadId"),
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "snippet": full.get("snippet", ""),
            })

        return json.dumps({"count": len(out), "messages": out}, indent=2)
    except Exception as e:
        if "401" in str(e) or "403" in str(e):
            _maybe_refresh_token_on_401("gmail")
        return json.dumps({"error": str(e)}, indent=2)


def gmail_get_message(message_id: str) -> str:
    """Get a Gmail message by ID with full body.

    Args:
        message_id: The Gmail message ID

    Returns:
        JSON string with full message content.
    """
    try:
        service = _get_service("gmail", "v1", "gmail")
        msg = service.users().messages().get(userId="me", id=message_id).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        body = _extract_gmail_body(msg.get("payload", {})) or msg.get("snippet", "")

        return json.dumps({
            "id": msg["id"],
            "threadId": msg.get("threadId"),
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "body": body,
            "labelIds": msg.get("labelIds", []),
        }, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("gmail")
        return json.dumps({"error": str(e)}, indent=2)


def gmail_get_unread_count() -> str:
    """Get the number of unread Gmail messages in the inbox.

    Returns:
        JSON string with unread count.
    """
    try:
        service = _get_service("gmail", "v1", "gmail")
        result = (
            service.users()
            .messages()
            .list(userId="me", q="is:unread in:inbox", maxResults=1)
            .execute()
        )
        return json.dumps({"unread_count": result.get("resultSizeEstimate", 0)}, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("gmail")
        return json.dumps({"error": str(e)}, indent=2)


def gmail_list_labels() -> str:
    """List all Gmail labels for the authenticated user.

    Returns:
        JSON string with labels.
    """
    try:
        service = _get_service("gmail", "v1", "gmail")
        result = service.users().labels().list(userId="me").execute()
        labels = [
            {"id": lbl["id"], "name": lbl["name"], "type": lbl.get("type", "")}
            for lbl in result.get("labels", [])
        ]
        return json.dumps({"count": len(labels), "labels": labels}, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("gmail")
        return json.dumps({"error": str(e)}, indent=2)


def _build_mime_message(to: str, subject: str, body: str, cc: str = "", reply_to_msg: dict | None = None) -> str:
    """Build a base64-encoded MIME message for sending.

    Args:
        to: Comma-separated recipient emails
        subject: Email subject line
        body: Plain text body
        cc: Comma-separated CC recipients (optional)
        reply_to_msg: Original message dict for reply threading

    Returns:
        Base64url-encoded raw message string.
    """
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc

    if reply_to_msg:
        msg["In-Reply-To"] = reply_to_msg.get("message_id", "")
        msg["References"] = reply_to_msg.get("message_id", "")

    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def gmail_send(to: str, subject: str, body: str, cc: str = "") -> str:
    """Send a new email.

    Args:
        to: Recipient email address(es), comma-separated
        subject: Subject line
        body: Plain text body
        cc: CC recipient(s), comma-separated (optional)

    Returns:
        JSON string with sent message info.
    """
    try:
        raw = _build_mime_message(to, subject, body, cc)
        service = _get_service("gmail", "v1", "gmail")
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return json.dumps({
            "id": sent["id"],
            "threadId": sent.get("threadId", ""),
            "labelIds": sent.get("labelIds", []),
            "status": "sent",
        }, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("gmail")
        return json.dumps({"error": str(e)}, indent=2)


def gmail_reply(message_id: str, body: str, reply_all: bool = False) -> str:
    """Reply to an existing email in the same thread.

    Args:
        message_id: The Gmail message ID to reply to
        body: Reply body text
        reply_all: If True, CC all recipients from original (default: False)

    Returns:
        JSON string with sent message info.
    """
    try:
        # Get original message for threading headers
        service = _get_service("gmail", "v1", "gmail")
        original = service.users().messages().get(userId="me", id=message_id).execute()
        headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}

        thread_id = original.get("threadId", message_id)
        subject = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        to = headers.get("From", "")

        thread_msg = {
            "message_id": headers.get("Message-ID", headers.get("Message-Id", "")),
        }

        cc = ""
        if reply_all and headers.get("Cc"):
            # Include CC recipients but exclude self
            cc = headers.get("Cc", "")

        raw = _build_mime_message(to, subject, body, cc, reply_to_msg=thread_msg)
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw, "threadId": thread_id})
            .execute()
        )
        return json.dumps({
            "id": sent["id"],
            "threadId": sent.get("threadId", ""),
            "to": to,
            "subject": subject,
            "status": "sent",
        }, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("gmail")
        return json.dumps({"error": str(e)}, indent=2)


def _extract_gmail_body(payload: dict[str, Any]) -> str | None:
    """Recursively extract the body text from a Gmail message payload."""
    import base64

    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            try:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            except Exception:
                return None

    if payload.get("mimeType", "").startswith("multipart/"):
        for part in payload.get("parts", []):
            body = _extract_gmail_body(part)
            if body:
                return body
        # fallback: decode first part's data
        for part in payload.get("parts", []):
            data = part.get("body", {}).get("data")
            if data:
                try:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except Exception:
                    pass

    return None


# ── Calendar ──


def calendar_list() -> str:
    """List all calendars the user can access.

    Returns:
        JSON string with calendar list.
    """
    try:
        service = _get_service("calendar", "v3", "calendar")
        result = service.calendarList().list().execute()
        calendars = [
            {
                "id": cal["id"],
                "summary": cal.get("summary", ""),
                "primary": cal.get("primary", False),
                "timeZone": cal.get("timeZone", ""),
                "accessRole": cal.get("accessRole", ""),
            }
            for cal in result.get("items", [])
        ]
        return json.dumps({"count": len(calendars), "calendars": calendars}, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("calendar")
        return json.dumps({"error": str(e)}, indent=2)


def calendar_upcoming(max_results: int = 10, calendar_id: str = "primary") -> str:
    """List upcoming events from a calendar.

    Args:
        max_results: Maximum events to return (max 50)
        calendar_id: Calendar ID (defaults to primary)

    Returns:
        JSON string with events.
    """
    try:
        service = _get_service("calendar", "v3", "calendar")
        max_results = max(1, min(max_results, 50))
        now = datetime.now(timezone.utc).isoformat()
        result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
        events = [
            {
                "id": ev["id"],
                "summary": ev.get("summary", "(no title)"),
                "start": ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", "")),
                "end": ev.get("end", {}).get("dateTime", ev.get("end", {}).get("date", "")),
                "location": ev.get("location", ""),
                "hangoutLink": ev.get("hangoutLink", ""),
            }
            for ev in result.get("items", [])
        ]
        return json.dumps({"count": len(events), "events": events}, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("calendar")
        return json.dumps({"error": str(e)}, indent=2)


def calendar_create_event(
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> str:
    """Create a calendar event.

    Args:
        summary: Event title
        start_time: Start time in RFC3339 format (e.g. '2026-06-26T14:00:00')
        end_time: End time in RFC3339 format
        description: Event description (optional)
        location: Event location (optional)
        calendar_id: Calendar ID (defaults to primary)

    Returns:
        JSON string with created event details.
    """
    try:
        service = _get_service("calendar", "v3", "calendar")
        event = {
            "summary": summary,
            "start": {"dateTime": start_time, "timeZone": "UTC"},
            "end": {"dateTime": end_time, "timeZone": "UTC"},
        }
        if description:
            event["description"] = description
        if location:
            event["location"] = location

        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        return json.dumps({
            "id": created["id"],
            "summary": created.get("summary", ""),
            "start": str(created.get("start", {}).get("dateTime", "")),
            "end": str(created.get("end", {}).get("dateTime", "")),
            "htmlLink": created.get("htmlLink", ""),
        }, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("calendar")
        return json.dumps({"error": str(e)}, indent=2)


def calendar_get_event(event_id: str, calendar_id: str = "primary") -> str:
    """Get details of a specific calendar event.

    Args:
        event_id: The event ID
        calendar_id: Calendar ID (defaults to primary)

    Returns:
        JSON string with event details.
    """
    try:
        service = _get_service("calendar", "v3", "calendar")
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return json.dumps({
            "id": event["id"],
            "summary": event.get("summary", ""),
            "description": event.get("description", ""),
            "start": str(event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))),
            "end": str(event.get("end", {}).get("dateTime", event.get("end", {}).get("date", ""))),
            "location": event.get("location", ""),
            "hangoutLink": event.get("hangoutLink", ""),
            "status": event.get("status", ""),
            "creator": event.get("creator", {}).get("email", ""),
            "attendees": [
                {"email": a["email"], "responseStatus": a.get("responseStatus", "")}
                for a in event.get("attendees", [])
            ],
            "htmlLink": event.get("htmlLink", ""),
        }, indent=2)
    except Exception as e:
        if "401" in str(e):
            _maybe_refresh_token_on_401("calendar")
        return json.dumps({"error": str(e)}, indent=2)


# ── Tool registration for MCP ──
# We use a dict so Ares can build MCP tool definitions dynamically

TOOL_DEFINITIONS = [
    {
        "name": "gmail_search",
        "description": "Search Gmail messages. Uses Gmail search syntax like 'is:unread', 'from:someone@example.com', 'after:2024/1/1'",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max results (1-50)", "default": 20},
            },
        },
    },
    {
        "name": "gmail_get_message",
        "description": "Get a complete Gmail message by ID including full body text",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The Gmail message ID"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "gmail_send",
        "description": "Send a new email from your Gmail account",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address(es), comma-separated for multiple"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Plain text email body"},
                "cc": {"type": "string", "description": "CC recipient(s), comma-separated (optional)"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "gmail_reply",
        "description": "Reply to an existing email in the same thread. Provide the message_id of the email to reply to.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The Gmail message ID to reply to"},
                "body": {"type": "string", "description": "Reply body text"},
                "reply_all": {"type": "boolean", "description": "If True, CC all recipients from original email (default: False)"},
            },
            "required": ["message_id", "body"],
        },
    },
    {
        "name": "gmail_get_unread_count",
        "description": "Get the number of unread Gmail messages in the inbox",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "gmail_list_labels",
        "description": "List all Gmail labels/folders",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "calendar_list",
        "description": "List all Google Calendars the user can access",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "calendar_upcoming",
        "description": "List upcoming events from a Google Calendar",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Max events (1-50)", "default": 10},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: primary)", "default": "primary"},
            },
        },
    },
    {
        "name": "calendar_create_event",
        "description": "Create a new Google Calendar event",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start_time": {"type": "string", "description": "Start time in RFC3339 format, e.g. '2026-06-26T14:00:00'"},
                "end_time": {"type": "string", "description": "End time in RFC3339 format"},
                "description": {"type": "string", "description": "Event description (optional)"},
                "location": {"type": "string", "description": "Event location (optional)"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: primary)"},
            },
            "required": ["summary", "start_time", "end_time"],
        },
    },
    {
        "name": "calendar_get_event",
        "description": "Get details of a specific calendar event including attendees",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event ID"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: primary)"},
            },
            "required": ["event_id"],
        },
    },
]

FUNCTION_MAP = {
    "gmail_search": gmail_search,
    "gmail_get_message": gmail_get_message,
    "gmail_get_unread_count": gmail_get_unread_count,
    "gmail_list_labels": gmail_list_labels,
    "gmail_send": gmail_send,
    "gmail_reply": gmail_reply,
    "calendar_list": calendar_list,
    "calendar_upcoming": calendar_upcoming,
    "calendar_create_event": calendar_create_event,
    "calendar_get_event": calendar_get_event,
}


def call_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a tool call to the appropriate function.

    This is the entry point called by Ares's MCP client manager.
    """
    func = FUNCTION_MAP.get(tool_name)
    if func is None:
        return json.dumps({"error": f"Unknown tool: {tool_name}"}, indent=2)

    # Strip out None values and only pass arguments the function accepts
    kwargs = {k: v for k, v in arguments.items() if v is not None}
    return func(**kwargs)
