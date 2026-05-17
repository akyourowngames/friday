"use client";

import { ArrowRight, Code2, MessageSquare, Search, Target } from "lucide-react";
import { FormEvent, useMemo, useState } from "react";

const assistantName = "FRIDAY";

const navigationItems = ["Home", "Memory", "Skills", "Settings"];

const memoryItems = [
  { label: "Key Facts", Icon: Target },
  { label: "Recent Chats", Icon: MessageSquare }
];

const toolItems = [
  { label: "Web Search", Icon: Search },
  { label: "Code Interpreter", Icon: Code2 }
];

function SplitLetters({ value }: { value: string }) {
  return (
    <span className="split-letters" aria-label={value}>
      {value.split("").map((letter) => (
        <span aria-hidden="true" key={letter}>
          {letter}
        </span>
      ))}
    </span>
  );
}

function FeaturePanel({
  title,
  items,
  side
}: {
  title: string;
  items: typeof memoryItems;
  side: "left" | "right";
}) {
  return (
    <aside className={`feature-panel feature-panel-${side}`} aria-label={title}>
      <h2>{title}</h2>
      <div className="feature-list">
        {items.map(({ label, Icon }) => (
          <button className="feature-button" key={label} type="button">
            <Icon aria-hidden="true" size={23} strokeWidth={1.6} />
            <span>{label}</span>
          </button>
        ))}
      </div>
    </aside>
  );
}

function Waveform({ active }: { active: boolean }) {
  const bars = useMemo(
    () =>
      Array.from({ length: 48 }, (_, index) => ({
        id: index,
        delay: `${index * 42}ms`,
        height: `${8 + ((index * 17) % 30)}px`
      })),
    []
  );

  return (
    <div className={`waveform ${active ? "waveform-active" : ""}`} aria-hidden="true">
      <span className="wave-dot" />
      <div className="wave-line" />
      <div className="wave-bars">
        {bars.map((bar) => (
          <span
            key={bar.id}
            style={{
              animationDelay: bar.delay,
              height: bar.height
            }}
          />
        ))}
      </div>
      <div className="wave-line" />
      <span className="wave-dot" />
    </div>
  );
}

export default function Home() {
  const [message, setMessage] = useState("");
  const hasMessage = message.trim().length > 0;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage("");
  }

  return (
    <main className="page-shell">
      <header className="topbar">
        <a className="brand" href="#" aria-label={assistantName}>
          <SplitLetters value={assistantName} />
        </a>

        <nav className="nav-tabs" aria-label="Primary">
          {navigationItems.map((item) => (
            <a className={item === "Home" ? "active" : ""} href="#" key={item}>
              {item}
            </a>
          ))}
        </nav>

        <button className="status-orbit" type="button" aria-label="Assistant status">
          <span />
        </button>
      </header>

      <section className="assistant-stage" aria-label="FRIDAY assistant console">
        <FeaturePanel title="MEMORY" items={memoryItems} side="left" />

        <div className="center-stack">
          <div className="orb-field" aria-hidden="true">
            <div className="outer-ring" />
            <div className="tick-ring" />
            <div className="orb-glow" />
            <img className="orb-media" src="/assets/friday-orb.gif" alt="" />
          </div>

          <div className="assistant-copy">
            <h1>
              <SplitLetters value={assistantName} />
            </h1>
            <p>Hello. How can I help you today?</p>
          </div>

          <Waveform active={hasMessage} />
        </div>

        <FeaturePanel title="TOOLS" items={toolItems} side="right" />
      </section>

      <form className="message-form" onSubmit={handleSubmit}>
        <label className="sr-only" htmlFor="friday-message">
          Message FRIDAY
        </label>
        <input
          id="friday-message"
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Message FRIDAY..."
          type="text"
          value={message}
        />
        <button aria-label="Send message" type="submit">
          <ArrowRight aria-hidden="true" size={24} strokeWidth={1.9} />
        </button>
      </form>
    </main>
  );
}
