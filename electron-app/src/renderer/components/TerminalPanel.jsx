import { useEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import '@xterm/xterm/css/xterm.css';
import useTerminalStore from '../stores/terminalStore';

export default function TerminalPanel() {
  const containerRef = useRef(null);
  const termRef = useRef(null);
  const fitAddonRef = useRef(null);
  const unsubscribersRef = useRef([]);

  const {
    isConnected,
    writeToTerminal,
    resizeTerminal,
    setSelection,
    clearSelection,
  } = useTerminalStore();

  useEffect(() => {
    if (!containerRef.current) return;

    const term = new Terminal({
      theme: {
        background: '#0e0e12',
        foreground: '#d4d4d8',
        cursor: '#6B5B95',
        cursorAccent: '#0e0e12',
        selectionBackground: 'rgba(107, 91, 149, 0.3)',
        selectionForeground: '#ffffff',
        black: '#18181b',
        red: '#ef4444',
        green: '#22c55e',
        yellow: '#eab308',
        blue: '#3b82f6',
        magenta: '#a855f7',
        cyan: '#06b6d4',
        white: '#fafafa',
        brightBlack: '#71717a',
        brightRed: '#f87171',
        brightGreen: '#4ade80',
        brightYellow: '#facc15',
        brightBlue: '#60a5fa',
        brightMagenta: '#c084fc',
        brightCyan: '#22d3ee',
        brightWhite: '#ffffff',
      },
      fontFamily: '"Cascadia Code", "Fira Code", "JetBrains Mono", "Consolas", monospace',
      fontSize: 13,
      lineHeight: 1.35,
      cursorBlink: true,
      cursorStyle: 'bar',
      scrollback: 10000,
      allowProposedApi: true,
      drawBoldTextInBrightColors: true,
      minimumContrastRatio: 1,
    });

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon();

    term.loadAddon(fitAddon);
    term.loadAddon(webLinksAddon);

    term.open(containerRef.current);

    requestAnimationFrame(() => {
      fitAddon.fit();
      term.focus();
    });

    termRef.current = term;
    fitAddonRef.current = fitAddon;

    // ── Wire up data flow ────────────────────────────────────

    const dataDisposable = term.onData((data) => {
      window.aresDesktop?.logToFile(`Renderer: term.onData: data=${JSON.stringify(data)}`);
      writeToTerminal(data);
    });

    const aresDesktop = window.aresDesktop;
    if (aresDesktop?.terminal) {
      const unsubData = aresDesktop.terminal.onData((data) => {
        window.aresDesktop?.logToFile(`Renderer: PTY data received: length=${data.length}`);
        term.write(data);
      });
      unsubscribersRef.current.push(unsubData);
    }

    const selectionDisposable = term.onSelectionChange(() => {
      const selection = term.getSelection();
      if (!selection) {
        clearSelection();
        return;
      }

      const position = term.getSelectionPosition();
      if (!position) {
        clearSelection();
        return;
      }

      setSelection(selection, position.start.y, position.end.y);
    });

    const handleResize = () => {
      fitAddon.fit();
      const dims = fitAddon.proposeDimensions();
      if (dims) {
        resizeTerminal(dims.cols, dims.rows);
      }
    };

    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(containerRef.current);

    window.addEventListener('resize', handleResize);

    // Focus terminal when container is clicked
    const handleContainerClick = () => {
      term.focus();
    };
    const containerElem = containerRef.current;
    containerElem.addEventListener('click', handleContainerClick);

    return () => {
      window.removeEventListener('resize', handleResize);
      containerElem.removeEventListener('click', handleContainerClick);
      resizeObserver.disconnect();
      selectionDisposable.dispose();
      dataDisposable.dispose();
      unsubscribersRef.current.forEach((unsub) => unsub());
      unsubscribersRef.current = [];
      term.dispose();
      termRef.current = null;
      fitAddonRef.current = null;
    };
  }, []);

  // Focus and fit terminal when it connects
  useEffect(() => {
    window.aresDesktop?.logToFile(`Renderer: isConnected changed: isConnected=${isConnected}, termExists=${!!termRef.current}`);
    if (isConnected && termRef.current) {
      window.aresDesktop?.logToFile("Renderer: Terminal connected, fitting and focusing...");
      termRef.current.focus();
      if (fitAddonRef.current) {
        try {
          fitAddonRef.current.fit();
          const dims = fitAddonRef.current.proposeDimensions();
          if (dims) {
            window.aresDesktop?.logToFile(`Renderer: resizing terminal on connect to cols=${dims.cols}, rows=${dims.rows}`);
            resizeTerminal(dims.cols, dims.rows);
          }
        } catch (err) {
          window.aresDesktop?.logToFile(`Renderer: error fitting terminal on connect: ${err.message}`);
        }
      }
    }
  }, [isConnected, resizeTerminal]);

  // Listen for terminal clear events
  useEffect(() => {
    const handleClear = () => {
      if (termRef.current) {
        termRef.current.clear();
        termRef.current.focus();
      }
    };
    window.addEventListener('terminal:clear', handleClear);
    return () => window.removeEventListener('terminal:clear', handleClear);
  }, []);

  useEffect(() => {
    const term = termRef.current;
    if (!term) return;

    const keyDisposable = term.onKey(({ key, domEvent }) => {
      if (domEvent.ctrlKey && domEvent.key === 'Enter') {
        domEvent.preventDefault();
        const ref = useTerminalStore.getState().sendSelectionToChat();
        if (ref) {
          window.dispatchEvent(new CustomEvent('terminal:sendToChat', { detail: ref }));
        }
      }
    });

    return () => keyDisposable.dispose();
  }, [isConnected]);

  return (
    <div className="terminal-container" ref={containerRef} />
  );
}
