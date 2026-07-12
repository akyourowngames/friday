"""Twilio bidirectional Media Streams gateway for the local Ares voice loop.

Twilio sends 8 kHz mu-law JSON frames over a public WSS endpoint.  This
module converts complete caller turns to local PCM, lets the normal Ares agent
respond (and use its tools/memory), then returns mu-law frames to Twilio.  It
uses local Whisper plus Edge TTS by default and never selects an OpenAI model.

Terminate TLS at a reverse proxy or tunnel and configure its public ``wss://``
origin as ``telephony.media_stream_url``.  The process itself defaults to a
loopback listener, which is deliberately not internet-facing.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ares.telephony.manager import TelephonyManager


_TWILIO_SAMPLE_RATE = 8000
_WHISPER_SAMPLE_RATE = 16000
_SILENCE_RMS = 300
_END_TURN_SILENT_FRAMES = 35  # 700 ms at Twilio's usual 20-ms media cadence.
_MIN_TURN_BYTES = _TWILIO_SAMPLE_RATE * 2 // 3  # roughly 0.65 seconds of PCM16


def decode_twilio_media(payload: str) -> bytes:
    """Decode one Twilio base64 mu-law payload to 8 kHz PCM16 bytes."""
    encoded = np.frombuffer(base64.b64decode(payload.encode("ascii")), dtype=np.uint8)
    value = np.bitwise_not(encoded)
    magnitude = ((value & 0x0F).astype(np.int32) << 3) + 0x84
    magnitude <<= ((value & 0x70) >> 4).astype(np.int32)
    pcm = np.where((value & 0x80) != 0, 0x84 - magnitude, magnitude - 0x84)
    return pcm.astype("<i2").tobytes()


def encode_twilio_media(pcm16: bytes) -> str:
    """Encode 8 kHz PCM16 audio to the base64 mu-law payload Twilio expects."""
    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.int32)
    signs = np.where(samples < 0, 0x80, 0).astype(np.int32)
    magnitude = np.minimum(np.abs(samples), 32635) + 0x84
    exponent = np.clip(np.floor(np.log2(np.maximum(magnitude, 1))).astype(np.int32) - 7, 0, 7)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    encoded = np.bitwise_not(signs | (exponent << 4) | mantissa).astype(np.uint8)
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def pcm8k_to_float16k(pcm16: bytes) -> np.ndarray:
    """Resample mono 8 kHz PCM16 to float32 16 kHz for local Whisper."""
    if not pcm16:
        return np.array([], dtype=np.float32)
    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
    if len(samples) == 1:
        return np.repeat(samples, 2) / 32768.0
    target_length = len(samples) * _WHISPER_SAMPLE_RATE // _TWILIO_SAMPLE_RATE
    source_positions = np.arange(len(samples), dtype=np.float32)
    target_positions = np.linspace(0, len(samples) - 1, target_length, dtype=np.float32)
    return np.interp(target_positions, source_positions, samples).astype(np.float32) / 32768.0


def pcm16_rms(pcm16: bytes) -> float:
    """Return RMS energy for signed mono PCM16 without deprecated audioop."""
    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0


def pcm16_to_twilio_chunks(pcm16: bytes, *, chunk_bytes: int = 320) -> list[str]:
    """Turn 8 kHz PCM16 into Twilio-sized base64 mu-law media payloads."""
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    return [encode_twilio_media(pcm16[offset : offset + chunk_bytes]) for offset in range(0, len(pcm16), chunk_bytes)]


@dataclass
class _StreamState:
    stream_sid: str
    call_id: str
    caller_audio: bytearray = field(default_factory=bytearray)
    saw_speech: bool = False
    silent_frames: int = 0
    processing: bool = False


class TwilioMediaGateway:
    """Performs authenticated-by-session Twilio media turns for one Ares app."""

    def __init__(self, manager: TelephonyManager, *, transcriber: Any | None = None, tts: Any | None = None) -> None:
        self.manager = manager
        self._transcriber = transcriber
        self._tts = tts

    def _get_transcriber(self) -> Any:
        if self._transcriber is None:
            from ares.voice.stt import WhisperTranscriber

            config = getattr(self.manager.config, "voice", None)
            self._transcriber = WhisperTranscriber(
                getattr(config, "stt_model", "small"),
                language=getattr(config, "stt_language", ""),
            )
        return self._transcriber

    def _get_tts(self) -> Any:
        if self._tts is None:
            from ares.voice.tts import EdgeTTS

            config = getattr(self.manager.config, "voice", None)
            self._tts = EdgeTTS(getattr(config, "tts_voice", ""))
        return self._tts

    async def handle(self, websocket: Any) -> None:
        """Handle a single Twilio WebSocket until the provider closes it."""
        state: _StreamState | None = None
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = str(event.get("event") or "")
                if kind == "start":
                    state = self._start_state(event)
                    if state is None:
                        await websocket.close(code=1008, reason="Unknown Ares call session")
                        return
                elif kind == "media" and state is not None:
                    await self._handle_media(websocket, state, event)
                elif kind == "stop" and state is not None:
                    if state.caller_audio and not state.processing:
                        await self._respond_for_turn(websocket, state)
                    return
        finally:
            if state is not None:
                self.manager.interrupt(state.call_id)

    def _start_state(self, event: dict[str, Any]) -> _StreamState | None:
        start = event.get("start") or {}
        parameters = start.get("customParameters") or {}
        call_id = str(parameters.get("call_id") or "")
        stream_sid = str(start.get("streamSid") or event.get("streamSid") or "")
        if not call_id or not stream_sid or self.manager.store.get_call(call_id) is None:
            return None
        return _StreamState(stream_sid=stream_sid, call_id=call_id)

    async def _handle_media(self, websocket: Any, state: _StreamState, event: dict[str, Any]) -> None:
        payload = str((event.get("media") or {}).get("payload") or "")
        if not payload:
            return
        try:
            pcm16 = decode_twilio_media(payload)
        except (ValueError, TypeError, base64.binascii.Error):
            return
        state.caller_audio.extend(pcm16)
        if pcm16_rms(pcm16) >= _SILENCE_RMS:
            caller_interrupted_response = state.processing
            state.saw_speech = True
            state.silent_frames = 0
            if caller_interrupted_response:
                # A caller talking while Ares is speaking is a barge-in.
                # Do not interrupt ordinary caller speech before an Ares turn
                # has even begun; that would suppress every first response.
                self.manager.interrupt(state.call_id)
                await websocket.send(json.dumps({"event": "clear", "streamSid": state.stream_sid}))
        elif state.saw_speech:
            state.silent_frames += 1
        if state.saw_speech and state.silent_frames >= _END_TURN_SILENT_FRAMES and not state.processing:
            await self._respond_for_turn(websocket, state)

    async def _respond_for_turn(self, websocket: Any, state: _StreamState) -> None:
        if state.processing or len(state.caller_audio) < _MIN_TURN_BYTES:
            return
        state.processing = True
        audio = bytes(state.caller_audio)
        state.caller_audio.clear()
        state.saw_speech = False
        state.silent_frames = 0
        try:
            text = await asyncio.to_thread(self._get_transcriber().transcribe_samples, pcm8k_to_float16k(audio), _WHISPER_SAMPLE_RATE)
            if not text:
                return
            response = await self.manager.respond_to_transcript(state.call_id, text)
            if not response:
                return
            encoded = await self._get_tts().synthesize(response)
            from ares.voice.player import audio_bytes_to_pcm16

            pcm8k = await asyncio.to_thread(audio_bytes_to_pcm16, encoded, _TWILIO_SAMPLE_RATE)
            for payload in pcm16_to_twilio_chunks(pcm8k):
                await websocket.send(json.dumps({"event": "media", "streamSid": state.stream_sid, "media": {"payload": payload}}))
        finally:
            state.processing = False


async def run_twilio_media_gateway(*, host: str = "127.0.0.1", port: int = 8767) -> None:
    """Start the loopback Twilio Media Stream service.

    This command intentionally does not terminate TLS. Publish it through a
    WSS-capable reverse proxy/tunnel, then save that external origin in Ares.
    """
    import websockets

    from ares.agent import Agent
    from ares.config import load_config
    from ares.conversations import ConversationStore
    from ares.memory import MemoryStore

    config = load_config()
    if not config.telephony.enabled:
        raise RuntimeError("Telephony is disabled. Enable it in Ares settings first.")
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
    gateway = TwilioMediaGateway(manager)
    try:
        async with websockets.serve(gateway.handle, host, port, max_size=2**20):
            print(f"Ares Twilio media gateway listening on ws://{host}:{port}")
            await asyncio.Future()
    finally:
        manager.close()
        conversation_store.close()
        memory_store.close()
