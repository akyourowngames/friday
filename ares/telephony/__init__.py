"""Provider-backed phone-call and voice assistant support for Ares."""

from ares.telephony.call_session import TelephonyStore
from ares.telephony.manager import TelephonyManager
from ares.telephony.models import CallContact, CallDirection, CallSession, CallStatus
from ares.telephony.twilio_client import TwilioClient, TwilioError

# Lazy imports to avoid circular dependencies with LiveKit SDK
# These are available as: from ares.telephony.livekit_plugins import WhisperSTT, EdgeTTSPlugin

__all__ = [
    "CallContact",
    "CallDirection",
    "CallSession",
    "CallStatus",
    "TelephonyManager",
    "TelephonyStore",
    "TwilioClient",
    "TwilioError",
]
