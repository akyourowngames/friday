"""WSGI adapter for Twilio Voice and status callbacks.

Deploy this behind a public HTTPS reverse proxy, then set
``telephony.public_base_url`` to that external origin.  The adapter is kept
separate from Ares' desktop WebSocket server so Twilio's HTTP callbacks can be
hosted by any WSGI/ASGI process without changing the local desktop transport.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from io import BytesIO
from typing import Any, Callable
from urllib.parse import parse_qs


def telephony_readiness(config: Any) -> dict[str, Any]:
    """Return a redacted, actionable deployment check for phone callbacks.

    This performs no network access and deliberately exposes only booleans.
    It is safe to run in support diagnostics or CI logs.
    """
    telephony = getattr(config, "telephony", config)
    missing: list[str] = []
    if not bool(getattr(telephony, "enabled", False)):
        missing.append("telephony.enabled")
    if not str(getattr(telephony, "account_sid", "") or ""):
        missing.append("Twilio account SID")
    if not str(getattr(telephony, "auth_token", "") or ""):
        missing.append("Twilio auth token")
    if not str(getattr(telephony, "phone_number", "") or ""):
        missing.append("Twilio phone number")
    public_base_url = str(getattr(telephony, "public_base_url", "") or "")
    if not public_base_url.startswith("https://"):
        missing.append("public HTTPS webhook URL")
    media_stream_url = str(getattr(telephony, "media_stream_url", "") or "")
    if not media_stream_url.startswith("wss://"):
        missing.append("public WSS media gateway URL")
    return {
        "ready": not missing,
        "missing": missing,
        "twilio_credentials_configured": bool(
            getattr(telephony, "account_sid", "") and getattr(telephony, "auth_token", "")
        ),
    }


def validate_twilio_signature(url: str, values: dict[str, str], signature: str, auth_token: str) -> bool:
    """Validate Twilio's signed form callback without requiring its SDK."""
    if not auth_token or not signature:
        return False
    body = url + "".join(f"{key}{values[key]}" for key in sorted(values))
    expected = base64.b64encode(hmac.new(auth_token.encode("utf-8"), body.encode("utf-8"), hashlib.sha1).digest()).decode("ascii")
    return hmac.compare_digest(expected, signature)


class TwilioWebhookApp:
    """Tiny WSGI application forwarding verified callbacks to TelephonyManager."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "")
        raw = environ.get("wsgi.input", BytesIO()).read(int(environ.get("CONTENT_LENGTH") or 0)) if method == "POST" else b""
        parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        values = {key: items[-1] for key, items in parsed.items()}
        query = parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=True)
        call_id = (query.get("call_id") or [""])[-1]
        config = self.manager.telephony_config
        base = str(getattr(config, "public_base_url", "")).rstrip("/")
        url = f"{base}{path}" + (f"?{environ.get('QUERY_STRING')}" if environ.get("QUERY_STRING") else "")
        signature = str(environ.get("HTTP_X_TWILIO_SIGNATURE") or "")
        auth_token = str(getattr(self.manager.twilio, "auth_token", "") or getattr(config, "auth_token", ""))
        if not validate_twilio_signature(url, values, signature, auth_token):
            start_response("403 Forbidden", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Invalid Twilio signature"]
        if path == getattr(config, "voice_webhook_path", "/telephony/twilio/voice"):
            session = self.manager.receive_incoming_call(values.get("From", ""), values.get("To", ""), call_sid=values.get("CallSid", ""))
            # Return a simple TwiML response to answer the call
            twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Hello</Say></Response>'
            start_response("200 OK", [("Content-Type", "application/xml; charset=utf-8")])
            return [twiml.encode("utf-8")]
        if path == getattr(config, "status_webhook_path", "/telephony/twilio/status"):
            if not call_id:
                session = self.manager.store.get_call_by_sid(values.get("CallSid", ""))
                call_id = session.call_id if session else ""
            if not call_id:
                start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
                return [b"Unknown call session"]
            self.manager.handle_provider_status(call_id, values)
            start_response("204 No Content", [])
            return [b""]
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found"]


def run_twilio_webhook_server(*, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the signed Twilio callback service for a local Ares installation.

    A reverse proxy or tunnel must provide the public HTTPS URL configured in
    ``telephony.public_base_url``.  The built-in WSGI server intentionally
    binds locally by default and is suitable for development/tunnel use.
    """
    from wsgiref.simple_server import make_server

    from ares.agent import Agent
    from ares.config import load_config
    from ares.context.conversations import ConversationStore
    from ares.memory import MemoryStore

    config = load_config()
    readiness = telephony_readiness(config)
    # The server can start before a media gateway exists, so callbacks and
    # signatures can be smoke-tested.  A call itself still fails closed if its
    # WSS media URL has not been supplied.
    if not readiness["twilio_credentials_configured"]:
        raise RuntimeError("Twilio account SID and auth token are required before starting the webhook server.")

    memory_store = MemoryStore()
    conversation_store = ConversationStore()
    agent = Agent(
        memory_store=memory_store,
        conversation_store=conversation_store,
        api_key=config.api_key,
        base_url=config.api_base_url,
        model=config.model,
        config=config,
        is_voice_session=True,
    )
    manager = agent.tool_executor.telephony
    if manager is None:
        raise RuntimeError("Telephony manager was not initialized.")
    server = make_server(host, port, TwilioWebhookApp(manager))
    print(f"Ares Twilio webhook listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        # This mode is synchronous; all underlying close operations are local.
        manager.close()
        conversation_store.close()
        memory_store.close()
