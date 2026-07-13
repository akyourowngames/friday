"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AresMessage, ConnectionState, JsonRecord } from "./types";

interface RuntimeDescriptor { websocket_url?: string; watcher_dashboard_url?: string }

export function useAresSocket(onMessage: (message: AresMessage) => void) {
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [runtime, setRuntime] = useState<RuntimeDescriptor>({});
  const socketRef = useRef<WebSocket | null>(null);
  const callbackRef = useRef(onMessage);
  const reconnectRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);
  const connectRef = useRef<() => void>(() => undefined);

  useEffect(() => { callbackRef.current = onMessage; }, [onMessage]);

  const connect = useCallback(async () => {
    if (timerRef.current) clearTimeout(timerRef.current);
    const existing = socketRef.current;
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) existing.close();
    setConnection("connecting");
    let descriptor: RuntimeDescriptor = {};
    try {
      const response = await fetch("/api/runtime", { cache: "no-store" });
      if (response.ok) descriptor = await response.json() as RuntimeDescriptor;
    } catch { /* Next dev mode uses the deterministic local fallback. */ }
    const host = typeof window !== "undefined" ? window.location.hostname || "127.0.0.1" : "127.0.0.1";
    const url = descriptor.websocket_url || `ws://${host}:8765`;
    setRuntime(descriptor);
    const socket = new WebSocket(url);
    socketRef.current = socket;
    socket.onopen = () => {
      reconnectRef.current = 0;
      setConnection("online");
      callbackRef.current({ type: "socket_open" });
    };
    socket.onmessage = (event) => {
      try { callbackRef.current(JSON.parse(String(event.data)) as AresMessage); }
      catch { callbackRef.current({ type: "error", message: "Ares sent an unreadable response." }); }
    };
    socket.onerror = () => setConnection("offline");
    socket.onclose = () => {
      if (socketRef.current === socket) socketRef.current = null;
      setConnection("offline");
      if (stoppedRef.current) return;
      const delay = Math.min(12000, 800 * 2 ** reconnectRef.current++);
      timerRef.current = setTimeout(() => connectRef.current(), delay);
    };
  }, []);

  useEffect(() => { connectRef.current = () => void connect(); }, [connect]);

  useEffect(() => {
    stoppedRef.current = false;
    timerRef.current = setTimeout(() => connectRef.current(), 0);
    return () => {
      stoppedRef.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      socketRef.current?.close();
    };
  }, [connect]);

  const send = useCallback((payload: JsonRecord & { type: string }) => {
    if (socketRef.current?.readyState !== WebSocket.OPEN) return false;
    socketRef.current.send(JSON.stringify(payload));
    return true;
  }, []);

  return { connection, runtime, send, reconnect: connect };
}
