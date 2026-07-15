"""Safe standalone LiveKit JWT token generation for Ares voice clients."""

from __future__ import annotations

import argparse
import datetime
import json
import os
from dataclasses import dataclass
from typing import Any

from livekit import api


@dataclass(frozen=True, slots=True)
class LiveKitCredentials:
    """Credentials kept in-process only; never serialize or log this object."""

    url: str
    api_key: str
    api_secret: str


def resolve_livekit_credentials(config: Any | None = None) -> LiveKitCredentials:
    """Load LiveKit credentials from Ares config first, then environment variables."""
    telephony = getattr(config, "telephony", config) if config is not None else None
    url = str(getattr(telephony, "livekit_url", "") or os.environ.get("LIVEKIT_URL", "")).strip()
    api_key = str(getattr(telephony, "livekit_api_key", "") or os.environ.get("LIVEKIT_API_KEY", "")).strip()
    api_secret = str(getattr(telephony, "livekit_api_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")).strip()
    if not url or not api_key or not api_secret:
        raise ValueError(
            "LiveKit API key and secret are required, along with LIVEKIT_URL. Set LIVEKIT_URL, "
            "LIVEKIT_API_KEY, and LIVEKIT_API_SECRET or save them under telephony in Ares config."
        )
    return LiveKitCredentials(url=url, api_key=api_key, api_secret=api_secret)


def _credentials(api_key: str, api_secret: str, config: Any | None = None) -> tuple[str, str]:
    if api_key and api_secret:
        return api_key, api_secret
    credentials = resolve_livekit_credentials(config)
    return api_key or credentials.api_key, api_secret or credentials.api_secret


def _ttl_seconds(value: int) -> int:
    return max(60, min(int(value), 86_400))


def generate_room_token(
    identity: str,
    room_name: str,
    *,
    api_key: str = "",
    api_secret: str = "",
    config: Any | None = None,
    ttl: int = 3600,
    can_publish: bool = True,
    can_subscribe: bool = True,
) -> str:
    """Generate a LiveKit access token for a user to join a room.

    Args:
        identity: Unique user identity (e.g. "user-123" or "guest").
        room_name: Name of the LiveKit room to join.
        api_key: LiveKit API key. Falls back to LIVEKIT_API_KEY env var.
        api_secret: LiveKit API secret. Falls back to LIVEKIT_API_SECRET env var.
        ttl: Token time-to-live in seconds (default 1 hour).
        can_publish: Allow publishing audio/video tracks.
        can_subscribe: Allow subscribing to other tracks.

    Returns:
        JWT string to pass to the LiveKit client SDK.
    """
    api_key, api_secret = _credentials(api_key, api_secret, config)
    if not identity.strip() or not room_name.strip():
        raise ValueError("identity and room_name are required")
    ttl = _ttl_seconds(ttl)

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=can_publish,
                can_subscribe=can_subscribe,
                can_publish_data=True,
            )
        )
        .with_ttl(datetime.timedelta(seconds=ttl))
    )

    return token.to_jwt()


def generate_agent_token(
    room_name: str,
    *,
    api_key: str = "",
    api_secret: str = "",
    config: Any | None = None,
    ttl: int = 3600,
) -> str:
    """Generate a LiveKit access token for the agent to join a room.

    The agent token has admin-level permissions to manage the room.
    """
    api_key, api_secret = _credentials(api_key, api_secret, config)
    if not room_name.strip():
        raise ValueError("room_name is required")

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity("ares-agent")
        .with_name("Ares Agent")
        .with_kind("agent")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                room_admin=True,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_ttl(datetime.timedelta(seconds=_ttl_seconds(ttl)))
    )

    return token.to_jwt()


def main(argv: list[str] | None = None) -> None:
    """Print a client or agent JWT without ever exposing configured credentials."""
    parser = argparse.ArgumentParser(description="Generate a short-lived Ares LiveKit access token")
    parser.add_argument("--room", required=True, help="LiveKit room name")
    parser.add_argument("--identity", default="ares-user", help="Participant identity for a user token")
    parser.add_argument("--agent", action="store_true", help="Generate an Ares agent token instead of a user token")
    parser.add_argument("--ttl", type=int, default=600, help="Token lifetime in seconds (60-86400; default 600)")
    parser.add_argument("--json", action="store_true", help="Emit room/identity/token metadata as JSON")
    args = parser.parse_args(argv)
    ttl = _ttl_seconds(args.ttl)

    if args.agent:
        token = generate_agent_token(args.room, ttl=ttl)
        identity = "ares-agent"
    else:
        token = generate_room_token(args.identity, args.room, ttl=ttl)
        identity = args.identity
    if args.json:
        print(json.dumps({"room": args.room, "identity": identity, "token": token, "ttl_seconds": ttl}))
    else:
        print(token)


if __name__ == "__main__":
    main()
