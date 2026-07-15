"use client";

import { AudioLines, Headphones, LoaderCircle, Mic, MicOff, PhoneOff, Radio, Volume2, X } from "lucide-react";
import { Room, RoomEvent, Track, TrackEvent } from "livekit-client";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";

type VoiceSession = {
  livekit_url: string;
  room: string;
  identity: string;
  token: string;
  expires_at: string;
};

type VoicePhase = "idle" | "connecting" | "connected" | "error";
type TranscriptItem = { id: string; role: "system" | "operator" | "ares"; text: string };
const MAX_RECONNECT_ATTEMPTS = 5;

export type VoiceConversationHandle = {
  start: () => Promise<void>;
};

type Props = {
  open: boolean;
  onClose: () => void;
};

function errorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : "Voice connection could not start.";
  if (/permission|notallowed|denied/i.test(message)) return "Microphone permission is blocked. Allow it for this tab, then try again.";
  return message;
}

export const VoiceConversation = forwardRef<VoiceConversationHandle, Props>(function VoiceConversation({ open, onClose }, ref) {
  const roomRef = useRef<Room | null>(null);
  const connectingRef = useRef(false);
  const intentionalDisconnectRef = useRef(false);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const startRef = useRef<(automatic?: boolean) => Promise<void>>(async () => {});
  const audioOutputRef = useRef<HTMLDivElement>(null);
  const audioElementsRef = useRef<Set<HTMLMediaElement>>(new Set());
  const [phase, setPhase] = useState<VoicePhase>("idle");
  const [status, setStatus] = useState("Ready for a secure voice connection.");
  const [muted, setMuted] = useState(false);
  const [agentSpeaking, setAgentSpeaking] = useState(false);
  const [audioBlocked, setAudioBlocked] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptItem[]>([
    { id: "ready", role: "system", text: "Tap the microphone and speak naturally. Ares answers in the same room." },
  ]);

  const addTranscript = useCallback((role: TranscriptItem["role"], text: string) => {
    const clean = text.replace(/\s+/g, " ").trim();
    if (!clean) return;
    setTranscript(current => [...current.slice(-39), { id: `${Date.now()}-${Math.random().toString(16).slice(2)}`, role, text: clean }]);
  }, []);

  const clearAudio = useCallback(() => {
    for (const element of audioElementsRef.current) element.remove();
    audioElementsRef.current.clear();
    audioOutputRef.current?.replaceChildren();
    setAgentSpeaking(false);
  }, []);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
    reconnectTimerRef.current = null;
  }, []);

  const scheduleReconnect = useCallback((detail = "Voice connection lost.") => {
    if (intentionalDisconnectRef.current || reconnectTimerRef.current !== null) return;
    const attempt = reconnectAttemptsRef.current + 1;
    if (attempt > MAX_RECONNECT_ATTEMPTS) {
      setPhase("error");
      setStatus("Voice could not reconnect. Tap Try again to create a fresh room session.");
      addTranscript("system", "Automatic reconnect stopped after five attempts.");
      return;
    }
    reconnectAttemptsRef.current = attempt;
    const delay = Math.min(8_000, 1_000 * (2 ** (attempt - 1)));
    setPhase("connecting");
    setStatus(`${detail} Reconnecting (${attempt}/${MAX_RECONNECT_ATTEMPTS})…`);
    addTranscript("system", `Voice connection interrupted. Retrying in ${Math.ceil(delay / 1_000)} second${delay === 1_000 ? "" : "s"}.`);
    reconnectTimerRef.current = window.setTimeout(() => {
      reconnectTimerRef.current = null;
      if (!intentionalDisconnectRef.current) void startRef.current(true);
    }, delay);
  }, [addTranscript]);

  const disconnect = useCallback(async (nextStatus = "Voice conversation ended.") => {
    intentionalDisconnectRef.current = true;
    clearReconnectTimer();
    const room = roomRef.current;
    roomRef.current = null;
    connectingRef.current = false;
    if (room) {
      room.removeAllListeners();
      try {
        await room.disconnect();
      } catch {
        // A dropped connection is already reflected in the UI state below.
      }
    }
    clearAudio();
    setMuted(false);
    setAudioBlocked(false);
    setPhase("idle");
    setStatus(nextStatus);
  }, [clearAudio, clearReconnectTimer]);

  const enableAudio = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;
    try {
      await room.startAudio();
      await Promise.all([...audioElementsRef.current].map(element => element.play()));
      setAudioBlocked(false);
      setStatus("Ares audio is enabled.");
    } catch {
      setAudioBlocked(true);
      setStatus("Browser audio is still blocked. Allow sound for this tab, then try again.");
    }
  }, []);

  const start = useCallback(async (automatic = false) => {
    if (connectingRef.current || roomRef.current) return;
    if (!automatic) reconnectAttemptsRef.current = 0;
    intentionalDisconnectRef.current = false;
    clearReconnectTimer();
    connectingRef.current = true;
    setPhase("connecting");
    setMuted(false);
    setAudioBlocked(false);
    setStatus("Creating a short-lived local voice session…");
    try {
      const response = await fetch("/api/voice/session", { cache: "no-store", credentials: "same-origin" });
      const payload = await response.json() as VoiceSession & { detail?: string };
      if (!response.ok) throw new Error(payload.detail || "Ares could not create a voice session.");

      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;
      room.on(RoomEvent.Connected, () => {
        const reconnected = reconnectAttemptsRef.current > 0;
        reconnectAttemptsRef.current = 0;
        setPhase("connected");
        setStatus("Voice channel live. Ares is listening.");
        addTranscript("system", reconnected ? "Voice channel restored. Continue speaking." : "Connected securely. Start speaking.");
      });
      room.on(RoomEvent.Reconnecting, () => setStatus("Reconnecting the voice channel…"));
      room.on(RoomEvent.Reconnected, () => setStatus("Voice channel restored."));
      room.on(RoomEvent.Disconnected, () => {
        if (roomRef.current !== room) return;
        roomRef.current = null;
        clearAudio();
        setMuted(false);
        setAudioBlocked(false);
        if (intentionalDisconnectRef.current) {
          setPhase("idle");
          setStatus("Voice conversation ended.");
        } else {
          scheduleReconnect("Voice conversation disconnected.");
        }
      });
      room.on(RoomEvent.TrackSubscribed, async (track, publication) => {
        if (track.kind !== Track.Kind.Audio) return;
        const audio = track.attach();
        audio.autoplay = true;
        audio.muted = false;
        audio.volume = 1;
        audio.dataset.livekitTrack = publication.trackSid;
        audioOutputRef.current?.appendChild(audio);
        audioElementsRef.current.add(audio);
        setAgentSpeaking(true);
        try {
          await audio.play();
          setAudioBlocked(false);
        } catch {
          setAudioBlocked(true);
          setStatus("Click Enable audio to hear Ares.");
        }
        track.on(TrackEvent.Muted, () => setAgentSpeaking(false));
        track.on(TrackEvent.Unmuted, () => setAgentSpeaking(true));
      });
      room.on(RoomEvent.TrackUnsubscribed, track => {
        if (track.kind !== Track.Kind.Audio) return;
        track.detach().forEach(element => {
          audioElementsRef.current.delete(element);
          element.remove();
        });
        if (!audioElementsRef.current.size) setAgentSpeaking(false);
      });
      room.on(RoomEvent.TranscriptionReceived, (segments, participant) => {
        const role = participant?.identity.includes("ares") ? "ares" : "operator";
        for (const segment of segments) if (segment.final) addTranscript(role, segment.text);
      });

      await room.connect(payload.livekit_url, payload.token);
      if (roomRef.current !== room) return;
      // This is intentionally in the microphone button's async flow. The same
      // user gesture unlocks speakers for browsers with strict autoplay rules.
      try {
        await room.startAudio();
      } catch {
        setAudioBlocked(true);
      }
      await room.localParticipant.setMicrophoneEnabled(true);
      setStatus("Microphone active. Talk to Ares.");
    } catch (error) {
      const activeRoom = roomRef.current;
      roomRef.current = null;
      activeRoom?.removeAllListeners();
      try {
        await activeRoom?.disconnect();
      } catch { /* Nothing further is needed after a failed connection. */ }
      clearAudio();
      if (intentionalDisconnectRef.current) {
        setPhase("idle");
        setStatus("Voice conversation ended.");
      } else if (automatic || reconnectAttemptsRef.current > 0) {
        scheduleReconnect(`Reconnect failed: ${errorMessage(error)}`);
      } else {
        setPhase("error");
        setStatus(errorMessage(error));
      }
    } finally {
      connectingRef.current = false;
    }
  }, [addTranscript, clearAudio, clearReconnectTimer, scheduleReconnect]);

  const toggleMuted = useCallback(async () => {
    const room = roomRef.current;
    if (!room || phase !== "connected") return;
    const nextMuted = !muted;
    try {
      await room.localParticipant.setMicrophoneEnabled(!nextMuted);
      setMuted(nextMuted);
      setStatus(nextMuted ? "Microphone muted." : "Microphone active. Talk to Ares.");
    } catch {
      setStatus("Ares could not change the microphone state. Check browser permission.");
    }
  }, [muted, phase]);

  const close = useCallback(() => {
    void disconnect();
    onClose();
  }, [disconnect, onClose]);

  startRef.current = start;
  useImperativeHandle(ref, () => ({ start: () => start() }), [start]);
  useEffect(() => () => { void disconnect("Voice conversation ended."); }, [disconnect]);

  if (!open) return null;
  const live = phase === "connected";
  const busy = phase === "connecting";
  const indicator = agentSpeaking ? "speaking" : live ? "listening" : busy ? "connecting" : "idle";

  return <div className="voice-room-layer" role="dialog" aria-modal="true" aria-label="Ares voice conversation">
    <button className="voice-room-backdrop" onClick={close} aria-label="Close voice conversation" />
    <section className="voice-room-panel">
      <header className="voice-room-head">
        <div><span className="voice-room-kicker"><Radio />Live voice</span><h2>Ares voice channel</h2><p>Sarvam speech · LiveKit transport</p></div>
        <button className="voice-room-close" onClick={close} aria-label="Close voice conversation"><X /></button>
      </header>

      <div className={`voice-orb is-${indicator}`} aria-label={agentSpeaking ? "Ares is speaking" : live ? "Ares is listening" : "Voice connection status"}>
        <i /><i /><span>{agentSpeaking ? <AudioLines /> : live ? <Headphones /> : busy ? <LoaderCircle /> : <Mic />}</span>
      </div>
      <p className="voice-status" aria-live="polite">{status}</p>

      <div className="voice-transcript" aria-live="polite">
        {transcript.map(item => <article key={item.id} className={`voice-line is-${item.role}`}><span>{item.role === "ares" ? "Ares" : item.role === "operator" ? "You" : "System"}</span><p>{item.text}</p></article>)}
      </div>

      <div className="voice-controls">
        {live ? <button className={`voice-control ${muted ? "is-muted" : ""}`} onClick={() => void toggleMuted()}>{muted ? <MicOff /> : <Mic />}{muted ? "Unmute" : "Mute"}</button> : <button className="voice-control is-start" onClick={() => void start()} disabled={busy}>{busy ? <LoaderCircle /> : <Mic />}{busy ? "Connecting" : phase === "error" ? "Try again" : "Start voice"}</button>}
        {audioBlocked && <button className="voice-control" onClick={() => void enableAudio()}><Volume2 />Enable audio</button>}
        <button className="voice-control is-leave" onClick={close}><PhoneOff />Leave</button>
      </div>
      <div className="voice-output" ref={audioOutputRef} aria-hidden="true" />
      <p className="voice-note">Keep <code>ares-livekit dev</code> running while you use voice.</p>
    </section>
  </div>;
});

VoiceConversation.displayName = "VoiceConversation";
