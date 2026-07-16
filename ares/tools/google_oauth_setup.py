"""Google OAuth setup for Gmail and Calendar integration.

Usage:
    python -m ares.tools.google_oauth_setup

This script:
1. Opens a browser for Google OAuth consent
2. Stores tokens in ~/.ares/data/mcp_tokens/
3. Configures the Google MCP server in ~/.ares/config.json
"""

from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

TOKEN_DIR = Path("~/.ares/data/mcp_tokens").expanduser()
CONFIG_PATH = Path("~/.ares/config.json").expanduser()
TOKEN_PATH = TOKEN_DIR / "google.json"
GMAIL_TOKEN_PATH = TOKEN_DIR / "gmail.json"
CALENDAR_TOKEN_PATH = TOKEN_DIR / "calendar.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

# Default client ID for Ares (you should replace with your own)
DEFAULT_CLIENT_ID = ""
DEFAULT_CLIENT_SECRET = ""


def get_client_credentials() -> tuple[str, str]:
    """Get OAuth client credentials from config or environment."""
    import os
    
    # Try config first
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text())
            for server in config.get("mcp_servers", []):
                cid = server.get("oauth_client_id", "")
                csec = server.get("oauth_client_secret", "")
                if cid and csec:
                    return cid, csec
        except Exception:
            pass
    
    # Try environment
    cid = os.environ.get("GOOGLE_CLIENT_ID", DEFAULT_CLIENT_ID)
    csec = os.environ.get("GOOGLE_CLIENT_SECRET", DEFAULT_CLIENT_SECRET)
    if cid and csec:
        return cid, csec
    
    return "", ""


def setup_oauth(client_id: str = "", client_secret: str = ""):
    """Run the OAuth setup flow."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    
    # Use provided credentials or get from config/env
    if not client_id or not client_secret:
        client_id, client_secret = get_client_credentials()
    
    if not client_id or not client_secret:
        print("\n" + "="*60)
        print("GOOGLE OAUTH SETUP")
        print("="*60)
        print("\nYou need Google OAuth credentials first.")
        print("\n1. Go to: https://console.cloud.google.com/apis/credentials")
        print("2. Create a new project (or select existing)")
        print("3. Enable these APIs:")
        print("   - Gmail API")
        print("   - Google Calendar API")
        print("4. Create OAuth 2.0 credentials (Desktop app type)")
        print("5. Download the credentials JSON file")
        print("\n" + "-"*60)
        print("\nEnter your OAuth credentials:")
        client_id = input("Client ID: ").strip()
        client_secret = input("Client Secret: ").strip()
        
        if not client_id or not client_secret:
            print("\n❌ Client ID and Client Secret are required.")
            return False
    
    # Create a temporary credentials file for the OAuth flow
    creds_file = TOKEN_DIR / "temp_creds.json"
    creds_data = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    creds_file.write_text(json.dumps(creds_data, indent=2))
    
    try:
        print("\n🌐 Opening browser for Google OAuth consent...")
        print("   If the browser doesn't open, copy the URL below.\n")
        
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_file), 
            SCOPES
        )
        
        # Run local server for callback
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            access_type="offline",
        )
        
        # Save token
        token_data = {
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or []),
            "expires_at": creds.expiry.isoformat() if creds.expiry else "",
        }
        TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
        GMAIL_TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
        CALENDAR_TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
        
        print(f"\n✅ Tokens saved to: {TOKEN_DIR}")
        
        # Update config.json with Google MCP server
        _update_config(client_id, client_secret)
        
        print("\n" + "="*60)
        print("SETUP COMPLETE!")
        print("="*60)
        print("\nYou can now use Gmail and Calendar tools:")
        print("  - gmail_search, gmail_send, gmail_reply")
        print("  - calendar_list, calendar_upcoming, calendar_create_event")
        print("\nRestart Ares to load the new configuration.")
        
        return True
        
    except Exception as e:
        print(f"\n❌ OAuth failed: {e}")
        return False
    finally:
        # Clean up temp file
        creds_file.unlink(missing_ok=True)


def _update_config(client_id: str, client_secret: str):
    """Add Google MCP server to Ares config."""
    if not CONFIG_PATH.exists():
        print(f"\n⚠️  Config not found at {CONFIG_PATH}")
        return
    
    config = json.loads(CONFIG_PATH.read_text())
    servers = config.get("mcp_servers", [])
    
    # Check if google server already exists
    google_server = None
    for server in servers:
        if server.get("name") == "google-workspace":
            google_server = server
            break
    
    if not google_server:
        google_server = {
            "name": "google-workspace",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "ares.tools.google_mcp_server"],
            "oauth_client_id": client_id,
            "oauth_client_secret": client_secret,
        }
        servers.append(google_server)
        config["mcp_servers"] = servers
        CONFIG_PATH.write_text(json.dumps(config, indent=2))
        print(f"\n✅ Added google-workspace server to config")
    else:
        print(f"\n✅ google-workspace server already in config")


def verify_setup() -> bool:
    """Verify that Google OAuth is properly configured."""
    if not TOKEN_PATH.exists():
        print("❌ No Google tokens found. Run setup first.")
        return False
    
    try:
        token_data = json.loads(TOKEN_PATH.read_text())
        creds = Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
        )
        
        # Try to refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save refreshed token
            token_data["access_token"] = creds.token
            TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
        
        print("✅ Google OAuth tokens are valid")
        return True
        
    except Exception as e:
        print(f"❌ Token verification failed: {e}")
        return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Google OAuth setup for Ares")
    parser.add_argument("--verify", action="store_true", help="Verify existing setup")
    parser.add_argument("--client-id", help="OAuth Client ID")
    parser.add_argument("--client-secret", help="OAuth Client Secret")
    args = parser.parse_args()
    
    if args.verify:
        verify_setup()
    else:
        setup_oauth(args.client_id, args.client_secret)
