"use client";

import {
  ArrowLeft, Bot, Code2, FileStack, Globe2, HeartPulse, Network,
  Send, Settings2, Sparkles, UserRound,
} from "lucide-react";
import { useState } from "react";
import type {
  McpServer, McpState, Skill, WatcherMonitor, WatcherState,
  WorkspaceFile, WorkspaceSettings,
} from "@/lib/types";
import { FilesView } from "./FilesView";
import { McpView } from "./McpView";
import { SettingsView } from "./SettingsView";
import { SkillsView } from "./SkillsView";
import { WatchersView } from "./WatchersView";

type Section = "general" | "identity" | "personalization" | "telegram" | "browser" |
  "monitoring" | "watchers" | "files" | "skills" | "mcp" | "advanced";

interface Props {
  settings: WorkspaceSettings;
  saveSettings: (settings: WorkspaceSettings & { advanced_mode?: boolean }) => void;
  savingSettings: boolean;
  watchers: WatcherState;
  refreshWatchers: () => void;
  createWatcher: () => void;
  editWatcher: (monitor: WatcherMonitor) => void;
  watcherAction: (action: string, arguments_: Record<string, unknown>) => void;
  files: WorkspaceFile[];
  uploadFiles: (files: FileList | File[]) => void;
  attachFile: (file: WorkspaceFile) => void;
  removeFile: (file: WorkspaceFile) => void;
  uploading: boolean;
  skills: Skill[];
  categories: string[];
  selectedSkill?: Skill;
  selectSkill: (skill: Skill) => void;
  createSkill: () => void;
  draftSkill: () => void;
  editSkill: (skill: Skill) => void;
  removeSkill: (skill: Skill) => void;
  mcp: McpState;
  probeMcp: () => void;
  addMcp: () => void;
  editMcp: (server: McpServer) => void;
  reconnectMcp: (server: McpServer) => void;
  removeMcp: (server: McpServer) => void;
  onSectionChange: (section: Section) => void;
  close: () => void;
}

const groups: Array<{ label: string; items: Array<{ id: Section; label: string; icon: typeof Settings2 }> }> = [
  { label: "Your Ares", items: [
    { id: "general", label: "General", icon: Settings2 },
    { id: "identity", label: "Profile", icon: UserRound },
    { id: "personalization", label: "Personalization", icon: Sparkles },
  ] },
  { label: "Capabilities", items: [
    { id: "watchers", label: "Watchers", icon: HeartPulse },
    { id: "files", label: "Files", icon: FileStack },
    { id: "skills", label: "Skills", icon: Bot },
    { id: "mcp", label: "MCP servers", icon: Network },
  ] },
  { label: "Connections", items: [
    { id: "telegram", label: "Telegram", icon: Send },
    { id: "browser", label: "Browser & tools", icon: Globe2 },
    { id: "monitoring", label: "Monitoring", icon: HeartPulse },
    { id: "advanced", label: "Advanced", icon: Code2 },
  ] },
];

const configSections = new Set<Section>(["general", "identity", "personalization", "telegram", "browser", "monitoring", "advanced"]);

export function SettingsHub(props: Props) {
  const [section, setSection] = useState<Section>("general");
  const select = (next: Section) => { setSection(next); props.onSectionChange(next); };
  const label = groups.flatMap(group => group.items).find(item => item.id === section)?.label || "Settings";

  return <section className="settings-hub">
    <header className="settings-hub-header">
      <button className="back-to-chat" onClick={props.close}><ArrowLeft />Back to chat</button>
      <div><span>Settings</span><i>/</i><strong>{label}</strong></div>
    </header>
    <div className="settings-hub-body">
      <aside className="settings-hub-nav">
        <div className="settings-hub-title"><span className="brand-mark">A</span><div><strong>Settings</strong><small>Manage your Ares workspace</small></div></div>
        {groups.map(group => <div className="settings-nav-group" key={group.label}><p>{group.label}</p>{group.items.map(item => <button className={section === item.id ? "is-active" : ""} key={item.id} onClick={() => select(item.id)}><item.icon /><span>{item.label}</span></button>)}</div>)}
      </aside>
      <main className="settings-hub-content">
        {configSections.has(section) && <SettingsView key={`${section}-${JSON.stringify(props.settings)}`} settings={props.settings} save={props.saveSettings} saving={props.savingSettings} activeTab={section} embedded />}
        {section === "watchers" && <WatchersView state={props.watchers} refresh={props.refreshWatchers} onCreate={props.createWatcher} onEdit={props.editWatcher} onAction={props.watcherAction} />}
        {section === "files" && <FilesView files={props.files} upload={props.uploadFiles} attach={props.attachFile} remove={props.removeFile} uploading={props.uploading} />}
        {section === "skills" && <SkillsView skills={props.skills} categories={props.categories} selected={props.selectedSkill} select={props.selectSkill} create={props.createSkill} draft={props.draftSkill} edit={props.editSkill} remove={props.removeSkill} />}
        {section === "mcp" && <McpView state={props.mcp} probe={props.probeMcp} add={props.addMcp} edit={props.editMcp} reconnect={props.reconnectMcp} remove={props.removeMcp} />}
      </main>
    </div>
  </section>;
}
