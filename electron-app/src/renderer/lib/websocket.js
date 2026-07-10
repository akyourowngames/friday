export class AresWebSocket {
  constructor() {
    this.ws = null;
    this.listeners = new Map();
    this.serverUrl = "";
    this.reconnectTimer = null;
    this.shouldReconnect = true;
  }

  on(type, callback) {
    const listeners = this.listeners.get(type) || new Set();
    listeners.add(callback);
    this.listeners.set(type, listeners);
    return () => listeners.delete(callback);
  }

  emit(type, payload) {
    for (const callback of this.listeners.get(type) || []) {
      callback(payload);
    }
  }

  connect(serverUrl) {
    this.serverUrl = serverUrl;
    this.shouldReconnect = true;
    this.close(false);
    this.ws = new WebSocket(serverUrl);

    this.ws.addEventListener("open", () => {
      this.emit("open", { serverUrl });
      this.refreshState();
    });

    this.ws.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        this.emit("message", payload);
        if (payload.type) {
          this.emit(payload.type, payload);
        }
      } catch (error) {
        this.emit("socket_error", { message: error.message });
      }
    });

    this.ws.addEventListener("close", () => {
      this.emit("close", {});
      if (this.shouldReconnect) {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = setTimeout(() => this.connect(this.serverUrl), 1500);
      }
    });

    this.ws.addEventListener("error", () => {
      this.emit("socket_error", { message: "WebSocket connection failed" });
    });
  }

  close(allowReconnect = false) {
    clearTimeout(this.reconnectTimer);
    this.shouldReconnect = allowReconnect;
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  send(payload) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this.emit("error", { message: "Ares server is not connected yet" });
      return false;
    }
    this.ws.send(JSON.stringify(payload));
    return true;
  }

  refreshState() {
    this.send({ type: "list_sessions" });
    this.send({ type: "get_status" });
    this.send({ type: "get_memories" });
    this.send({ type: "get_context", query: "" });
    this.send({ type: "get_personal_settings" });
    this.send({ type: "get_onboarding_state" });
  }
}

export const aresSocket = new AresWebSocket();
