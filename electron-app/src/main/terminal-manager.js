'use strict';

/**
 * Manages PTY process lifecycle for the interactive terminal panel.
 * One active terminal session at a time.
 */
class TerminalManager {
  constructor() {
    this.ptyProcess = null;
    this.sessionId = null;
    this.mainWindow = null;
  }

  /**
   * Set the BrowserWindow reference (called after window creation).
   * @param {import('electron').BrowserWindow} win
   */
  setWindow(win) {
    this.mainWindow = win;
  }

  /**
   * Spawn a new PTY shell process.
   * @returns {{ sessionId: string }} The session ID.
   */
  create() {
    if (this.ptyProcess) {
      this.kill();
    }

    const pty = require('node-pty');

    let shell;
    let shellArgs = [];
    if (process.platform === 'win32') {
      shell = process.env.COMSPEC || 'powershell.exe';
      if (shell.toLowerCase().includes('powershell')) {
        shellArgs = ['-NoLogo'];
      }
    } else {
      shell = process.env.SHELL || '/bin/bash';
    }

    this.sessionId = this._generateId();

    this.ptyProcess = pty.spawn(shell, shellArgs, {
      name: 'xterm-256color',
      cols: 80,
      rows: 24,
      cwd: process.env.HOME || process.env.USERPROFILE,
      env: { ...process.env, TERM: 'xterm-256color' },
    });

    this.ptyProcess.onData((data) => {
      if (this.mainWindow && !this.mainWindow.isDestroyed()) {
        this.mainWindow.webContents.send('terminal:data', data);
      }
    });

    this.ptyProcess.onExit(({ exitCode, signal }) => {
      if (this.mainWindow && !this.mainWindow.isDestroyed()) {
        this.mainWindow.webContents.send('terminal:exit', { exitCode, signal });
      }
      this.ptyProcess = null;
      this.sessionId = null;
    });

    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send('terminal:create', { sessionId: this.sessionId });
    }

    return { sessionId: this.sessionId };
  }

  /**
   * Write data to the PTY (user input or command).
   * @param {string} data
   */
  write(data) {
    if (this.ptyProcess) {
      this.ptyProcess.write(data);
    }
  }

  /**
   * Resize the PTY dimensions.
   * @param {number} cols
   * @param {number} rows
   */
  resize(cols, rows) {
    if (this.ptyProcess) {
      try {
        this.ptyProcess.resize(cols, rows);
      } catch (e) {
        // Resize can fail if PTY is exiting
      }
    }
  }

  /**
   * Kill the active PTY process.
   */
  kill() {
    if (this.ptyProcess) {
      try {
        this.ptyProcess.kill();
      } catch (e) {
        // Process may already be dead
      }
      this.ptyProcess = null;
      this.sessionId = null;
    }
  }

  /**
   * Check if a PTY session is active.
   * @returns {boolean}
   */
  isActive() {
    return this.ptyProcess !== null;
  }

  /**
   * Generate a unique session ID.
   * @returns {string}
   */
  _generateId() {
    return 'term-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  }
}

module.exports = TerminalManager;
