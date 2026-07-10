import {
  AlertTriangle,
  Bot,
  Check,
  ChevronRight,
  Copy,
  FileCode2,
  Plus,
  Save,
  Search,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { aresSocket } from "../../lib/websocket.js";

const NEW_SKILL_SOURCE = `---
name: new-skill
description: Describe when Ares should use this skill.
category: general
version: 1.0.0
---

# New Skill

## When to use

Use this skill when...

## Workflow

1. Understand the request.
2. Complete the work safely.
3. Verify the result.
`;

export function SkillsPage() {
  const [skills, setSkills] = useState([]);
  const [categories, setCategories] = useState({});
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [selected, setSelected] = useState(null);
  const [mode, setMode] = useState("view");
  const [name, setName] = useState("");
  const [editorCategory, setEditorCategory] = useState("general");
  const [source, setSource] = useState("");
  const [goal, setGoal] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [drafting, setDrafting] = useState(false);

  function refresh(overrides = {}) {
    aresSocket.send({
      type: "list_skills",
      query: overrides.query ?? query,
      category: overrides.category ?? category,
    });
  }

  useEffect(() => {
    const offSkills = aresSocket.on("skills", (payload) => {
      setSkills(payload.skills || []);
      setCategories(payload.categories || {});
    });
    const offDetail = aresSocket.on("skill_detail", ({ skill }) => {
      setSelected(skill);
      setMode("view");
      setName(skill.name);
      setEditorCategory(skill.category || "general");
      setSource(skill.source || "");
      setError("");
    });
    const offSaved = aresSocket.on("skill_saved", ({ skill }) => {
      setSelected(skill);
      setMode("view");
      setName(skill.name);
      setEditorCategory(skill.category || "general");
      setSource(skill.source || "");
      setSaving(false);
      setNotice(`Saved ${skill.name}`);
      setError("");
    });
    const offDeleted = aresSocket.on("skill_deleted", () => {
      setSelected(null);
      setSource("");
      setNotice("Skill deleted");
    });
    const offDraft = aresSocket.on("skill_draft", (payload) => {
      setDrafting(false);
      setMode("create");
      setSelected(null);
      setName(payload.name || "new-skill");
      setEditorCategory(payload.category || "general");
      setSource(payload.source || NEW_SKILL_SOURCE);
      setNotice("Ares created a draft. Review it before saving.");
    });
    const offError = aresSocket.on("skills_error", ({ message }) => {
      setSaving(false);
      setDrafting(false);
      setError(message || "Skills operation failed");
    });
    refresh();
    return () => {
      offSkills();
      offDetail();
      offSaved();
      offDeleted();
      offDraft();
      offError();
    };
  }, []);

  const installedCount = useMemo(
    () => Object.values(categories).reduce((total, count) => total + Number(count || 0), 0),
    [categories]
  );

  function openSkill(skill) {
    setError("");
    setNotice("");
    aresSocket.send({ type: "get_skill", name: skill.name });
  }

  function startNew() {
    setSelected(null);
    setMode("create");
    setName("new-skill");
    setEditorCategory(category || "general");
    setSource(NEW_SKILL_SOURCE);
    setError("");
    setNotice("");
  }

  function save() {
    if (!name.trim() || !source.trim()) {
      setError("A skill name and SKILL.md content are required.");
      return;
    }
    setSaving(true);
    setError("");
    aresSocket.send({
      type: mode === "create" ? "create_skill" : "update_skill",
      name: name.trim(),
      category: editorCategory.trim() || "general",
      source,
    });
  }

  function draftWithAres() {
    if (!goal.trim()) {
      setError("Tell Ares what the skill should help with.");
      return;
    }
    setDrafting(true);
    setError("");
    setNotice("");
    aresSocket.send({
      type: "draft_skill",
      name: name.trim() || "new-skill",
      category: editorCategory.trim() || "general",
      goal: goal.trim(),
    });
  }

  function deleteSkill() {
    if (!selected?.editable) return;
    if (window.confirm(`Delete ${selected.name}? This cannot be undone.`)) {
      aresSocket.send({ type: "delete_skill", name: selected.name });
    }
  }

  return (
    <section className="skills-page">
      <aside className="skills-catalog">
        <div className="skills-catalog-head">
          <div>
            <span className="eyebrow">Capability library</span>
            <h2>Skills</h2>
            <p>{installedCount} installed workflows</p>
          </div>
          <button className="primary-icon-btn" type="button" title="Create skill" onClick={startNew}>
            <Plus size={18} />
          </button>
        </div>

        <label className="skills-search">
          <Search size={15} />
          <input
            value={query}
            placeholder="Search skills"
            onChange={(event) => {
              const next = event.target.value;
              setQuery(next);
              refresh({ query: next });
            }}
          />
        </label>

        <div className="skill-category-row">
          <button
            className={!category ? "active" : ""}
            type="button"
            onClick={() => { setCategory(""); refresh({ category: "" }); }}
          >
            All
          </button>
          {Object.entries(categories).map(([item, count]) => (
            <button
              className={category === item ? "active" : ""}
              type="button"
              key={item}
              onClick={() => { setCategory(item); refresh({ category: item }); }}
            >
              {item} <span>{count}</span>
            </button>
          ))}
        </div>

        <div className="skill-list">
          {skills.map((skill) => (
            <button
              className={`skill-list-item${selected?.name === skill.name ? " active" : ""}`}
              type="button"
              key={skill.name}
              onClick={() => openSkill(skill)}
            >
              <span className="skill-list-icon"><Sparkles size={16} /></span>
              <span>
                <strong>{skill.name}</strong>
                <small>{skill.description}</small>
                <em>{skill.category} · v{skill.version}</em>
              </span>
              <ChevronRight size={15} />
            </button>
          ))}
          {!skills.length ? <div className="skills-empty-list">No matching skills</div> : null}
        </div>
      </aside>

      <div className="skill-workspace">
        <div className="skill-builder">
          <div className="skill-builder-title">
            <span><Bot size={16} /></span>
            <div>
              <strong>Build with Ares</strong>
              <small>Describe a capability and get a complete editable SKILL.md.</small>
            </div>
          </div>
          <textarea
            value={goal}
            rows={2}
            placeholder="Example: Review pull requests for security risks and verify every finding…"
            onChange={(event) => setGoal(event.target.value)}
          />
          <button type="button" onClick={draftWithAres} disabled={drafting}>
            <Sparkles size={15} /> {drafting ? "Drafting…" : "Generate draft"}
          </button>
        </div>

        {error ? <div className="skill-banner error"><AlertTriangle size={15} />{error}<button onClick={() => setError("")}><X size={13} /></button></div> : null}
        {notice ? <div className="skill-banner success"><Check size={15} />{notice}<button onClick={() => setNotice("")}><X size={13} /></button></div> : null}

        {selected || mode === "create" ? (
          <div className="skill-editor-card">
            <div className="skill-editor-head">
              <div>
                <span className="eyebrow">{mode === "create" ? "New user skill" : selected?.editable ? "User skill" : "Built-in skill"}</span>
                <h2>{name || "Untitled skill"}</h2>
                {selected ? <p>{selected.description}</p> : <p>Review the generated instructions, then save them to your local skill library.</p>}
              </div>
              <div className="skill-editor-actions">
                {selected && !selected.editable && mode !== "create" ? (
                  <button type="button" onClick={() => setMode("create")}><Copy size={15} /> Customize</button>
                ) : null}
                {selected?.editable && mode !== "create" ? (
                  <button className="danger" type="button" onClick={deleteSkill}><Trash2 size={15} /> Delete</button>
                ) : null}
                {(mode === "create" || selected?.editable) ? (
                  <button className="primary" type="button" onClick={save} disabled={saving}>
                    <Save size={15} /> {saving ? "Saving…" : "Save skill"}
                  </button>
                ) : null}
              </div>
            </div>

            <div className="skill-fields">
              <label>
                <span>Name</span>
                <input value={name} disabled={mode !== "create"} onChange={(event) => setName(event.target.value)} />
              </label>
              <label>
                <span>Category</span>
                <input value={editorCategory} disabled={mode !== "create"} onChange={(event) => setEditorCategory(event.target.value)} />
              </label>
              {selected ? <div className="skill-path"><FileCode2 size={14} /><span>{selected.path}</span></div> : null}
            </div>

            {selected?.lint_messages?.length ? (
              <div className="skill-lint">
                <strong>Quality checks</strong>
                {selected.lint_messages.map((message) => <span key={message}><AlertTriangle size={13} />{message}</span>)}
              </div>
            ) : null}

            <label className="skill-source-label">
              <span>SKILL.md</span>
              <textarea
                className="skill-source-editor"
                value={source}
                readOnly={mode !== "create" && !selected?.editable}
                spellCheck={false}
                onChange={(event) => setSource(event.target.value)}
              />
            </label>
          </div>
        ) : (
          <div className="skill-empty-state">
            <span><Sparkles size={30} /></span>
            <h2>Select a skill to inspect it</h2>
            <p>Browse installed capabilities, edit your own workflows, customize built-ins, or ask Ares to create one.</p>
            <button type="button" onClick={startNew}><Plus size={16} /> Create manually</button>
          </div>
        )}
      </div>
    </section>
  );
}
