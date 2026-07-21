"""A local callback OAuth flow for a user-owned GitHub OAuth App."""

from __future__ import annotations

import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


@dataclass(frozen=True)
class OAuthToken:
    access_token: str


@dataclass(frozen=True)
class DeviceAuthorization:
    verification_uri: str
    user_code: str


def authorization_url(client_id: str, callback_url: str, state: str) -> str:
    return "https://github.com/login/oauth/authorize?" + urlencode({
        "client_id": client_id, "redirect_uri": callback_url, "state": state,
    })


def authorize_github_oauth(
    *,
    client_id: str,
    client_secret: str,
    callback_url: str,
    timeout_seconds: float = 180.0,
    on_authorization_url: Callable[[str], None] | None = None,
) -> OAuthToken:
    """Open the user browser, validate the local callback, then exchange its code."""
    parsed = urlparse(callback_url)
    if not client_id or not client_secret:
        raise ValueError("A GitHub OAuth client ID and client secret are required.")
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        raise ValueError("The callback must be an http://127.0.0.1:<port>/ URL.")

    state, result, completed = secrets.token_urlsafe(32), {}, threading.Event()
    expected_path = parsed.path or "/"

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request = urlparse(self.path)
            if request.path != expected_path:
                self.send_error(404)
                return
            query = parse_qs(request.query)
            result.update({key: str((query.get(key) or [""])[0]) for key in ("code", "state", "error")})
            body = b"Ares connected to GitHub Copilot. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            completed.set()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer((parsed.hostname, parsed.port), CallbackHandler)
    server.timeout = 0.5
    try:
        url = authorization_url(client_id, callback_url, state)
        if on_authorization_url is not None:
            on_authorization_url(url)
        webbrowser.open(url, new=2)
        deadline = time.monotonic() + timeout_seconds
        while not completed.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not completed.is_set():
        raise TimeoutError("GitHub authorization timed out; run `/copilot login` again.")
    if result.get("state") != state:
        raise ValueError("GitHub OAuth state verification failed.")
    if result.get("error"):
        raise ValueError(f"GitHub authorization was declined: {result['error']}")
    if not result.get("code"):
        raise ValueError("GitHub did not return an authorization code.")

    response = httpx.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        json={"client_id": client_id, "client_secret": client_secret, "code": result["code"], "redirect_uri": callback_url},
        timeout=30.0,
    )
    response.raise_for_status()
    token = str(response.json().get("access_token") or "")
    if not token:
        raise ValueError("GitHub did not return an OAuth access token.")
    return OAuthToken(access_token=token)


def authorize_github_device_flow(
    *,
    client_id: str,
    on_device_code: Callable[[DeviceAuthorization], None] | None = None,
) -> OAuthToken:
    """Authorize a headless/CLI client with GitHub's device flow.

    A device-flow enabled GitHub OAuth App needs only its public client ID.
    GitHub supplies a short user code and verification URL, then this function
    polls at GitHub's requested interval until the user approves or expires.
    """
    if not client_id:
        raise ValueError("A GitHub OAuth client ID is required.")
    response = httpx.post(
        "https://github.com/login/device/code",
        headers={"Accept": "application/json"},
        json={"client_id": client_id},
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json()
    device_code = str(payload.get("device_code") or "")
    user_code = str(payload.get("user_code") or "")
    verification_uri = str(payload.get("verification_uri_complete") or payload.get("verification_uri") or "")
    if not (device_code and user_code and verification_uri):
        raise ValueError(str(payload.get("error_description") or payload.get("error") or "GitHub did not return a device code."))

    if on_device_code is not None:
        on_device_code(DeviceAuthorization(verification_uri=verification_uri, user_code=user_code))
    webbrowser.open(verification_uri, new=2)

    interval = max(1, int(payload.get("interval") or 5))
    deadline = time.monotonic() + max(1, int(payload.get("expires_in") or 900))
    while time.monotonic() < deadline:
        time.sleep(interval)
        token_response = httpx.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=30.0,
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        token = str(token_payload.get("access_token") or "")
        if token:
            return OAuthToken(access_token=token)
        error = str(token_payload.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error:
            raise ValueError(str(token_payload.get("error_description") or error))
    raise TimeoutError("GitHub device authorization expired; run `/copilot login` again.")


__all__ = [
    "DeviceAuthorization",
    "OAuthToken",
    "authorization_url",
    "authorize_github_device_flow",
    "authorize_github_oauth",
]
