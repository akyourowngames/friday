"use client";

import { Headphones, X } from "lucide-react";
import { forwardRef, useImperativeHandle } from "react";

export type VoiceConversationHandle = {
  start: () => Promise<void>;
};

type Props = {
  open: boolean;
  onClose: () => void;
};

export const VoiceConversation = forwardRef<VoiceConversationHandle, Props>(function VoiceConversation({ open, onClose }, ref) {
  useImperativeHandle(ref, () => ({ start: async () => {} }));

  if (!open) return null;

  return <div className="voice-room-layer" role="dialog" aria-modal="true" aria-label="Ares voice conversation">
    <button className="voice-room-backdrop" onClick={onClose} aria-label="Close voice conversation" />
    <section className="voice-room-panel">
      <header className="voice-room-head">
        <div><span className="voice-room-kicker"><Headphones />Voice</span><h2>Voice unavailable</h2><p>Voice features have been removed from this build.</p></div>
        <button className="voice-room-close" onClick={onClose} aria-label="Close voice conversation"><X /></button>
      </header>
      <p className="voice-status">Voice conversation is not available. LiveKit integration has been removed.</p>
    </section>
  </div>;
});

VoiceConversation.displayName = "VoiceConversation";
