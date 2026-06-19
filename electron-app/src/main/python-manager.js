const { spawn } = require("child_process");
const crypto = require("crypto");
const net = require("net");
const path = require("path");

class PythonManager {
  constructor({ host = "127.0.0.1", port = 8765 } = {}) {
    this.host = host;
    this.port = port;
    this.process = null;
    this.ready = false;
  }

  get url() {
    return `ws://${this.host}:${this.port}`;
  }

  async start() {
    if (this.process) {
      return this.url;
    }

    const python = process.env.ARES_PYTHON || "python";
    const repoRoot = process.env.ARES_REPO_ROOT || path.resolve(__dirname, "..", "..", "..");
    const args = ["-m", "ares", "--server", "--host", this.host, "--port", String(this.port)];

    this.process = spawn(python, args, {
      cwd: repoRoot,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"]
    });

    this.process.stdout.on("data", (data) => {
      process.stdout.write(`[ares-server] ${data}`);
    });

    this.process.stderr.on("data", (data) => {
      process.stderr.write(`[ares-server] ${data}`);
    });

    this.process.on("exit", (code, signal) => {
      this.ready = false;
      this.process = null;
      process.stdout.write(`[ares-server] exited code=${code} signal=${signal}\n`);
    });

    await this.waitForPort(15000);
    this.ready = true;
    return this.url;
  }

  async restart() {
    await this.stop();
    return this.start();
  }

  stop() {
    return new Promise((resolve) => {
      if (!this.process) {
        resolve();
        return;
      }

      const child = this.process;
      const timeout = setTimeout(() => {
        if (child.exitCode === null) {
          child.kill("SIGKILL");
        }
      }, 2500);

      child.once("exit", () => {
        clearTimeout(timeout);
        resolve();
      });
      child.kill();
    });
  }

  waitForPort(timeoutMs) {
    const startedAt = Date.now();
    return new Promise((resolve, reject) => {
      const attempt = () => {
        const socket = net.createConnection(this.port, this.host);
        const key = crypto.randomBytes(16).toString("base64");
        const request = [
          "GET / HTTP/1.1",
          `Host: ${this.host}:${this.port}`,
          "Upgrade: websocket",
          "Connection: Upgrade",
          `Sec-WebSocket-Key: ${key}`,
          "Sec-WebSocket-Version: 13",
          "",
          ""
        ].join("\r\n");

        let response = "";
        socket.once("connect", () => {
          socket.write(request);
        });
        socket.on("data", (data) => {
          response += data.toString("utf8");
          if (response.includes("101 Switching Protocols")) {
            socket.end();
            resolve();
          }
        });
        socket.once("connect", () => {
          socket.setTimeout(1000);
        });
        socket.once("timeout", () => {
          socket.destroy();
          retryOrFail();
        });
        socket.once("error", () => {
          socket.destroy();
          retryOrFail();
        });

        function retryOrFail() {
          if (Date.now() - startedAt > timeoutMs) {
            reject(new Error(`Ares server did not open ${this.host}:${this.port}`));
            return;
          }
          setTimeout(attempt, 250);
        }
      };
      attempt();
    });
  }
}

module.exports = { PythonManager };
