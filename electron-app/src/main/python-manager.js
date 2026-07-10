const { spawn } = require("child_process");
const net = require("net");
const path = require("path");
const fs = require("fs");

class PythonManager {
  constructor({ host = "127.0.0.1", port = 8765 } = {}) {
    this.host = host;
    this.port = port;
    this.process = null;
    this.ready = false;
    this.startPromise = null;
  }

  get url() {
    return `ws://${this.host}:${this.port}`;
  }

  _resolveServerCommand() {
    const isPackaged = !process.env.ELECTRON_DEV && fs.existsSync(path.join(process.resourcesPath || "", "ares-server", "ares-server.exe"));

    if (isPackaged) {
      const exePath = path.join(process.resourcesPath, "ares-server", "ares-server.exe");
      return { command: exePath, args: ["--host", this.host, "--port", String(this.port)], cwd: undefined };
    }

    const python = process.env.ARES_PYTHON || "python";
    const repoRoot = process.env.ARES_REPO_ROOT || path.resolve(__dirname, "..", "..", "..");
    return { command: python, args: ["-m", "ares", "--server", "--host", this.host, "--port", String(this.port)], cwd: repoRoot };
  }

  async start() {
    if (this.ready && this.process) {
      return this.url;
    }

    if (this.startPromise) {
      return this.startPromise;
    }

    if (this.process) {
      await this.stop();
    }

    this.startPromise = this._start();
    try {
      return await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  async _start() {

    if (this.port === 8765) {
      this.port = await this.getFreePort();
    }

    const { command, args, cwd } = this._resolveServerCommand();

    const child = spawn(command, args, {
      cwd,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"]
    });
    this.process = child;

    const readyPromise = this.waitForReadyOutput(30000);

    child.stdout.on("data", (data) => {
      const text = data.toString();
      process.stdout.write(`[ares-server] ${text}`);
      this._handleReadyOutput(text);
    });

    child.stderr.on("data", (data) => {
      process.stderr.write(`[ares-server] ${data.toString()}`);
    });

    child.on("error", (error) => {
      this._rejectReady(error);
    });

    child.on("exit", (code, signal) => {
      const isCurrentProcess = this.process === child;
      if (isCurrentProcess) {
        this.ready = false;
        this.process = null;
      }
      process.stdout.write(`[ares-server] exited code=${code} signal=${signal}\n`);
      if (isCurrentProcess) {
        this._rejectReady(new Error(`Ares server exited before startup (code=${code}, signal=${signal})`));
      }
    });

    try {
      await readyPromise;
      this.ready = true;
      return this.url;
    } catch (error) {
      await this.stop();
      throw error;
    }
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
      const pid = child.pid;
      this.process = null;
      this.ready = false;

      if (process.platform === "win32") {
        const { exec } = require("child_process");
        exec(`taskkill /pid ${pid} /T /F`, () => {
          resolve();
        });
        return;
      }

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

  waitForReadyOutput(timeoutMs) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._readyWaiter = null;
        reject(new Error(`Ares server did not announce startup for ${this.host}:${this.port}`));
      }, timeoutMs);

      this._readyWaiter = {
        resolve: () => {
          clearTimeout(timer);
          this._readyWaiter = null;
          resolve();
        },
        reject: (error) => {
          clearTimeout(timer);
          this._readyWaiter = null;
          reject(error);
        },
      };
    });
  }

  _handleReadyOutput(text) {
    if (this._readyWaiter && text.includes("Ares desktop server listening")) {
      this._readyWaiter.resolve();
    }
  }

  _rejectReady(error) {
    if (this._readyWaiter) {
      this._readyWaiter.reject(error);
    }
  }

  getFreePort() {
    return new Promise((resolve, reject) => {
      const srv = net.createServer();
      srv.listen(0, this.host, () => {
        const port = srv.address().port;
        srv.close(() => resolve(port));
      });
      srv.on("error", reject);
    });
  }
}

module.exports = { PythonManager };
