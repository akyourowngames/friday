"use client";

import { Bot, CheckCircle2, Code2, Edit3, FileCode2, Plus, Search, Sparkles, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import type { Skill } from "@/lib/types";

interface Props {
  skills: Skill[];
  categories: string[];
  selected?: Skill;
  select: (skill: Skill) => void;
  create: () => void;
  draft: () => void;
  edit: (skill: Skill) => void;
  remove: (skill: Skill) => void;
}

export function SkillsView({ skills, categories, selected, select, create, draft, edit, remove }: Props) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const visible = useMemo(() => skills.filter(skill => (!category || skill.category === category) && `${skill.name} ${skill.description} ${skill.category}`.toLowerCase().includes(query.toLowerCase())), [skills, query, category]);
  return <section className="view is-active"><div className="page-scroll">
    <div className="page-head"><div><p className="eyebrow">BEHAVIOR &amp; PROCEDURE LAYER</p><h1>Skills studio</h1><p>Inspect built-in expertise, create private operating procedures, or have Ares draft one from intent.</p></div><div className="head-actions"><button className="secondary-btn" onClick={draft}><Sparkles />Draft with Ares</button><button className="primary-btn" onClick={create}><Plus />New skill</button></div></div>
    <div className="catalog-toolbar"><div className="search-field"><Search /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search skills, triggers, and categories" /></div><div className="skill-categories"><button className={!category ? "is-active" : ""} onClick={() => setCategory("")}>All</button>{categories.map(item => <button className={category === item ? "is-active" : ""} key={item} onClick={() => setCategory(item)}>{item}</button>)}</div></div>
    <div className="skill-layout"><div className="skill-grid">{visible.map(skill => <button className={`skill-card ${selected?.name === skill.name ? "is-active" : ""}`} onClick={() => select(skill)} key={skill.name}><div className="skill-card-top"><span className="skill-icon">{skill.model_invocable ? <Bot /> : <Code2 />}</span><span className="type-badge">{skill.editable ? "USER" : "CORE"}</span></div><h3>{skill.name.replaceAll("-", " ")}</h3><p>{skill.description || "No description supplied."}</p><div className="skill-card-foot"><span>{skill.category}</span><span>v{skill.version || "1.0"}</span></div></button>)}{!visible.length && <div className="empty-state">No skills match your search.</div>}</div>
      <aside className="detail-drawer">{selected ? <><div className="detail-head"><p className="panel-kicker">{selected.editable ? "PRIVATE SKILL" : "ARES CORE SKILL"}</p><h2>{selected.name.replaceAll("-", " ")}</h2><p>{selected.description}</p><div className="detail-actions">{selected.editable && <button className="secondary-btn" onClick={() => edit(selected)}><Edit3 />Edit source</button>}{selected.editable && <button className="danger-btn" onClick={() => remove(selected)}><Trash2 />Delete</button>}</div></div><div className="detail-body"><div className="detail-facts"><div><small>VERSION</small><strong>{selected.version || "1.0"}</strong></div><div><small>CATEGORY</small><strong>{selected.category}</strong></div><div><small>FILES</small><strong>{selected.files?.length || 1}</strong></div><div><small>STATUS</small><strong><CheckCircle2 size={11} /> READY</strong></div></div>{selected.source ? <pre className="source-preview">{selected.source}</pre> : <div className="empty-state compact"><div><FileCode2 /><p>Loading skill source…</p></div></div>}</div></> : <div className="empty-state"><div><Sparkles size={24} /><p>Select a skill to inspect its contract.</p></div></div>}</aside>
    </div>
  </div></section>;
}
