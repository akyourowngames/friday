import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  Bot,
  Check,
  Cpu,
  Database,
  FileText,
  RefreshCw,
  Save,
  Server,
  User,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore.js";
import { ModelSelector } from "./ModelSelector.jsx";

const SECTIONS = [
  { id: "profile", label: "Profile", icon: User },
  { id: "personality", label: "Ares Personality", icon: Bot },
  { id: "runtime", label: "Runtime", icon: Cpu },
  { id: "memory", label: "Memory", icon: Database },
];

function memoryText(memory) {
  if (!memory) {
    return "";
  }
  return memory.fact_text || memory.content || memory.text || JSON.stringify(memory);
}

function readProfileField(markdown, field) {
  const match = markdown.match(new RegExp(`^- ${field}:\\s*(.*)$`, "im"));
  return match ? match[1].trim() : "";
}

function writeProfileField(markdown, field, value) {
  const normalized = markdown || "# About Me\n\n## Identity\n";
  const line = `- ${field}: ${value.trim()}`;
  const fieldPattern = new RegExp(`^- ${field}:.*$`, "im");
  if (fieldPattern.test(normalized)) {
    return normalized.replace(fieldPattern, line);
  }
  if (/^## Identity\s*$/im.test(normalized)) {
    return normalized.replace(/^## Identity\s*$/im, `## Identity\n${line}`);
  }
  return `${normalized.trim()}\n\n## Identity\n${line}\n`;
}

function MarkdownEditor({ value, onChange, rows = 16 }) {
  return (
    <textarea
      className="settings-markdown"
      value={value}
      rows={rows}
      spellCheck={false}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function SettingsPage({
  onBack,
  onSetModel,
  onRefresh,
  onFetchPersonalSettings,
  onSavePersonalSettings,
}) {
  const [activeSection, setActiveSection] = useState("profile");
  const connected = useSettingsStore((s) => s.connected);
  const serverUrl = useSettingsStore((s) => s.serverUrl);
  const model = useSettingsStore((s) => s.model);
  const memoryCount = useSettingsStore((s) => s.memoryCount);
  const memories = useSettingsStore((s) => s.memories);
  const contextUsage = useSettingsStore((s) => s.contextUsage);
  const personalSettings = useSettingsStore((s) => s.personalSettings);
  const saveStatus = useSettingsStore((s) => s.personalSettingsStatus);
  const saveError = useSettingsStore((s) => s.personalSettingsError);
  const [profileDraft, setProfileDraft] = useState("");
  const [soulDraft, setSoulDraft] = useState("");

  useEffect(() => {
    onFetchPersonalSettings();
  }, [onFetchPersonalSettings]);

  useEffect(() => {
    setProfileDraft(personalSettings.profile.content || "");
  }, [personalSettings.profile.content]);

  useEffect(() => {
    setSoulDraft(personalSettings.soul.content || "");
  }, [personalSettings.soul.content]);

  const profileName = useMemo(() => readProfileField(profileDraft, "Name"), [profileDraft]);
  const pronouns = useMemo(() => readProfileField(profileDraft, "Pronouns"), [profileDraft]);
  const profileDirty = profileDraft !== (personalSettings.profile.content || "");
  const soulDirty = soulDraft !== (personalSettings.soul.content || "");

  function updateProfileField(field, value) {
    setProfileDraft((current) => writeProfileField(current, field, value));
  }

  function saveProfile() {
    onSavePersonalSettings("profile", profileDraft);
  }

  function saveSoul() {
    onSavePersonalSettings("soul", soulDraft);
  }

  return (
    <section className="settings-page">
      <aside className="settings-page-nav">
        <button className="settings-back" type="button" onClick={onBack}>
          <ArrowLeft size={16} />
          <span>Back to chat</span>
        </button>
        <div className="settings-nav-list">
          {SECTIONS.map((section) => {
            const Icon = section.icon;
            return (
              <button
                key={section.id}
                type="button"
                className={`settings-nav-item${activeSection === section.id ? " active" : ""}`}
                onClick={() => setActiveSection(section.id)}
              >
                <Icon size={15} />
                <span>{section.label}</span>
              </button>
            );
          })}
        </div>
      </aside>

      <div className="settings-page-content">
        <header className="settings-page-header">
          <div>
            <h1>Settings</h1>
            <p>Local Ares identity, personality, runtime, and memory controls.</p>
          </div>
          <button className="settings-refresh-inline" type="button" onClick={onRefresh}>
            <RefreshCw size={15} />
            <span>Refresh</span>
          </button>
        </header>

        {activeSection === "profile" ? (
          <div className="settings-card">
            <div className="settings-card-head">
              <div>
                <h2>Personal Profile</h2>
                <p>Ares reads this markdown for your name, preferences, projects, goals, and notes.</p>
              </div>
              <button className="settings-save" type="button" disabled={!profileDirty} onClick={saveProfile}>
                {saveStatus === "saving" && profileDirty ? <RefreshCw size={15} /> : <Save size={15} />}
                <span>{profileDirty ? "Save profile" : "Saved"}</span>
              </button>
            </div>
            <div className="settings-grid two">
              <label className="settings-label">
                <span>Name</span>
                <input
                  value={profileName}
                  placeholder="Your name"
                  onChange={(event) => updateProfileField("Name", event.target.value)}
                />
              </label>
              <label className="settings-label">
                <span>Pronouns</span>
                <input
                  value={pronouns}
                  placeholder="Optional"
                  onChange={(event) => updateProfileField("Pronouns", event.target.value)}
                />
              </label>
            </div>
            <div className="settings-path">
              <FileText size={13} />
              <span>{personalSettings.profile.path || "profile.md"}</span>
            </div>
            <MarkdownEditor value={profileDraft} onChange={setProfileDraft} />
          </div>
        ) : null}

        {activeSection === "personality" ? (
          <div className="settings-card">
            <div className="settings-card-head">
              <div>
                <h2>Ares Personality</h2>
                <p>Edit the soul markdown that defines how Ares talks, decides, and behaves.</p>
              </div>
              <button className="settings-save" type="button" disabled={!soulDirty} onClick={saveSoul}>
                {saveStatus === "saving" && soulDirty ? <RefreshCw size={15} /> : <Save size={15} />}
                <span>{soulDirty ? "Save personality" : "Saved"}</span>
              </button>
            </div>
            <div className="settings-path">
              <FileText size={13} />
              <span>{personalSettings.soul.path || "soul.md"}</span>
            </div>
            <MarkdownEditor value={soulDraft} onChange={setSoulDraft} rows={20} />
          </div>
        ) : null}

        {activeSection === "runtime" ? (
          <div className="settings-card">
            <div className="settings-card-head">
              <div>
                <h2>Runtime</h2>
                <p>Model and server connection state for this desktop session.</p>
              </div>
            </div>
            <div className="settings-section-block">
              <h3>Model</h3>
              <ModelSelector onSetModel={onSetModel} />
            </div>
            <div className="settings-runtime-grid">
              <div className="settings-runtime-tile">
                <Server size={15} />
                <span>Server</span>
                <strong>{serverUrl || "ws://127.0.0.1:8765"}</strong>
              </div>
              <div className={`settings-runtime-tile ${connected ? "online" : "offline"}`}>
                {connected ? <Wifi size={15} /> : <WifiOff size={15} />}
                <span>Connection</span>
                <strong>{connected ? "Connected" : "Reconnecting"}</strong>
              </div>
              <div className="settings-runtime-tile">
                <Cpu size={15} />
                <span>Model</span>
                <strong>{model}</strong>
              </div>
              <div className="settings-runtime-tile">
                <Database size={15} />
                <span>Context</span>
                <strong>{contextUsage.percent || 0}% used</strong>
              </div>
            </div>
          </div>
        ) : null}

        {activeSection === "memory" ? (
          <div className="settings-card">
            <div className="settings-card-head">
              <div>
                <h2>Memory</h2>
                <p>Recent local memories available to Ares. Memory edits still happen from chat commands/tools.</p>
              </div>
              <div className="settings-count-pill">{memoryCount} total</div>
            </div>
            <div className="settings-memory-list page">
              {memories.slice(0, 12).map((memory, index) => (
                <div className="settings-memory-row" key={memory.fact_id || memory.id || index}>
                  <span>{memoryText(memory)}</span>
                </div>
              ))}
              {!memories.length ? <div className="settings-empty">No memories stored yet</div> : null}
            </div>
          </div>
        ) : null}

        {saveStatus === "saved" ? (
          <div className="settings-toast">
            <Check size={14} />
            <span>Saved</span>
          </div>
        ) : null}
        {saveError ? <div className="settings-toast error">{saveError}</div> : null}
      </div>
    </section>
  );
}
