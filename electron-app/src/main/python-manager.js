const { spawn } = require("child_process");
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

    if (this.port === 8765) {
      this.port = await this.getFreePort();
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

  waitForPort(timeoutMs) {
    const startedAt = Date.now();
    return new Promise((resolve, reject) => {
      const attempt = () => {
        const socket = net.createConnection(this.port, this.host);
        let resolved = false;

        socket.once("connect", () => {
          resolved = true;
          socket.destroy();
          resolve();
        });

        socket.setTimeout(1000);
        socket.once("timeout", () => {
          socket.destroy();
          retryOrFail();
        });
        socket.once("error", () => {
          socket.destroy();
          retryOrFail();
        });

        function retryOrFail() {
          if (resolved) return;
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
