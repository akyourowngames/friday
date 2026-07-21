"""Minimal Twilio Voice REST adapter with no hard Twilio SDK dependency."""

from __future__ import annotations

import os
from html import escape
from typing import Any

import httpx


class TwilioError(RuntimeError):
    """A bounded provider error safe to display to a local caller."""


class TwilioClient:
    """Twilio Voice adapter used by :class:`TelephonyManager`.

    Keeping this HTTP-level client small makes the telephony domain testable
    with ``httpx.MockTransport`` and allows a later provider adapter without
    changing session, transcript, or UI code.
    """

    api_root = "https://api.twilio.com/2010-04-01"

    def __init__(self, config: Any, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self.account_sid = str(getattr(config, "account_sid", "") or os.getenv("TWILIO_ACCOUNT_SID", "")).strip()
        self.auth_token = str(getattr(config, "auth_token", "") or os.getenv("TWILIO_AUTH_TOKEN", "")).strip()
        self.phone_number = str(getattr(config, "phone_number", "") or os.getenv("TWILIO_PHONE_NUMBER", "")).strip()
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(20.0))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.phone_number)

    def _calls_url(self, call_sid: str | None = None) -> str:
        base = f"{self.api_root}/Accounts/{self.account_sid}/Calls"
        return f"{base}/{call_sid}.json" if call_sid else f"{base}.json"

    def _request(self, method: str, url: str, *, data: dict[str, str] | None = None, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise TwilioError("Twilio is not configured. Set account SID, auth token, and phone number in local telephony settings.")
        try:
            response = self.client.request(method, url, auth=(self.account_sid, self.auth_token), data=data, params=params)
        except httpx.HTTPError as exc:
            raise TwilioError(f"Twilio request failed: {exc}") from exc
        if response.is_error:
            try:
                message = str(response.json().get("message") or response.text)
            except ValueError:
                message = response.text
            raise TwilioError(f"Twilio {response.status_code}: {message[:300]}")
        try:
            return dict(response.json())
        except ValueError as exc:
            raise TwilioError("Twilio returned an invalid response.") from exc

    def make_call(self, number: str, *, voice_url: str, status_callback: str = "") -> dict[str, Any]:
        data = {"To": number, "From": self.phone_number, "Url": voice_url}
        if status_callback:
            data.update({"StatusCallback": status_callback, "StatusCallbackEvent": "initiated ringing answered completed"})
        return self._request("POST", self._calls_url(), data=data)

    def hangup(self, call_sid: str) -> dict[str, Any]:
        return self._request("POST", self._calls_url(call_sid), data={"Status": "completed"})

    def mute(self, call_sid: str, muted: bool = True) -> dict[str, Any]:
        """Mute a conference participant when Twilio is routing through one."""
        # A direct Twilio Call resource has no mute property.  Store the intent
        # locally and use the conference participant endpoint when callers
        # configure that architecture.
        return {"ok": False, "call_sid": call_sid, "muted": muted, "error": "Twilio mute requires a conference participant SID; configure a conference media bridge."}

    def get_call(self, call_sid: str) -> dict[str, Any]:
        return self._request("GET", self._calls_url(call_sid))

    def list_recent_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        payload = self._request("GET", self._calls_url(), params={"PageSize": str(max(1, min(int(limit), 100)))})
        return list(payload.get("calls") or [])

    def transfer(self, call_sid: str, *, destination: str) -> dict[str, Any]:
        twiml = f"<Response><Dial><Number>{escape(destination)}</Number></Dial></Response>"
        return self._request("POST", self._calls_url(call_sid), data={"Twiml": twiml})
