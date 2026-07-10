import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CirclePlus,
  Compass,
  Sparkles,
  Target,
  UserRound,
  X,
} from "lucide-react";
import { MODEL_REGISTRY, useSettingsStore } from "../../stores/settingsStore.js";
import { AresLogo } from "../common/AresLogo.jsx";

const STEPS = [
  { title: "Identity", detail: "A little about you", icon: UserRound },
  { title: "Direction", detail: "What matters now", icon: Target },
  { title: "Ares", detail: "Your working style", icon: Sparkles },
];

const PERSONALITIES = [
  { id: "jarvis", title: "Focused", copy: "Concise, warm, and decisive." },
  { id: "mentor", title: "Thoughtful", copy: "Explains the why and teaches." },
  { id: "buddy", title: "Easygoing", copy: "Friendly, relaxed, and practical." },
];

const WORK_STYLES = [
  "Clean & minimal",
  "Verbose & documented",
  "Pragmatic — whatever works",
];

const ASSISTANT_STYLES = [
  "Concise (Jarvis-style) — lead with answer, brief explanations",
  "Detailed — explain reasoning, show work",
  "Casual & friendly — relaxed, uses humor",
];

export function OnboardingPage({ onComplete }) {
  const savedModel = useSettingsStore((state) => state.model);
  const lastError = useSettingsStore((state) => state.lastError);
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [pronouns, setPronouns] = useState("");
  const [goalInput, setGoalInput] = useState("");
  const [goals, setGoals] = useState([]);
  const [codingStyle, setCodingStyle] = useState(WORK_STYLES[0]);
  const [assistantStyle, setAssistantStyle] = useState(ASSISTANT_STYLES[0]);
  const [personality, setPersonality] = useState("jarvis");
  const [model, setModel] = useState(savedModel);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (savedModel) setModel(savedModel);
  }, [savedModel]);

  useEffect(() => {
    if (submitting && lastError) {
      setSubmitting(false);
      setError(lastError);
    }
  }, [lastError, submitting]);

  const modelGroups = useMemo(() => Object.values(MODEL_REGISTRY), []);
  const activeStep = STEPS[step];

  function addGoal() {
    const goal = goalInput.trim();
    if (!goal || goals.includes(goal)) return;
    setGoals((items) => [...items, goal]);
    setGoalInput("");
  }

  function handleGoalKeyDown(event) {
    if (event.key === "Enter") {
      event.preventDefault();
      addGoal();
    }
  }

  function next() {
    if (step === 0 && !name.trim()) {
      setError("Tell Ares what to call you first.");
      return;
    }
    setError("");
    setStep((current) => Math.min(current + 1, STEPS.length - 1));
  }

  function finish() {
    setSubmitting(true);
    setError("");
    const sent = onComplete({
      name: name.trim(),
      pronouns: pronouns.trim(),
      goals,
      coding_style: codingStyle,
      assistant_style: assistantStyle,
      personality,
      model,
    });
    if (!sent) {
      setSubmitting(false);
      setError("Ares is reconnecting. Try again in a moment.");
    }
  }

  return (
    <main className="onboarding-shell">
      <div className="onboarding-ambient onboarding-ambient-one" />
      <div className="onboarding-ambient onboarding-ambient-two" />
      <div className="onboarding-grid" />

      <section className="onboarding-stage" aria-label="Set up Ares">
        <aside className="onboarding-hero">
          <div className="onboarding-mark-wrap">
            <div className="onboarding-orbit onboarding-orbit-one" />
            <div className="onboarding-orbit onboarding-orbit-two" />
            <div className="onboarding-mark">
              <AresLogo size={44} />
            </div>
          </div>
          <div className="onboarding-hero-copy">
            <span className="onboarding-kicker"><Sparkles size={13} /> Your personal Ares</span>
            <h1>Make it feel<br /><em>like yours.</em></h1>
            <p>A few thoughtful details give Ares the context to work beside you from the first conversation.</p>
          </div>
          <div className="onboarding-steps" aria-label={`Step ${step + 1} of ${STEPS.length}`}>
            {STEPS.map((item, index) => {
              const Icon = item.icon;
              return (
                <div className={`onboarding-step${index === step ? " active" : ""}${index < step ? " complete" : ""}`} key={item.title}>
                  <span className="onboarding-step-icon">{index < step ? <Check size={14} /> : <Icon size={14} />}</span>
                  <span><strong>{item.title}</strong><small>{item.detail}</small></span>
                </div>
              );
            })}
          </div>
          <div className="onboarding-local-note"><span /> Stays on this device</div>
        </aside>

        <section className="onboarding-form-panel">
          <div className="onboarding-form-heading">
            <span className="onboarding-count">0{step + 1} <i>/</i> 0{STEPS.length}</span>
            <div>
              <p>{activeStep.title}</p>
              <h2>{step === 0 ? "What should Ares call you?" : step === 1 ? "What are you moving toward?" : "Choose your defaults."}</h2>
              <span>{step === 0 ? "You can adjust any of this later in Settings." : step === 1 ? "Add a few goals now, or leave this open for later." : "Pick a model and a personality that fit your rhythm."}</span>
            </div>
          </div>

          {step === 0 ? (
            <div className="onboarding-fields onboarding-fields-enter">
              <label className="onboarding-label">
                <span>Your name</span>
                <input autoFocus value={name} placeholder="e.g. Krish" onChange={(event) => setName(event.target.value)} onKeyDown={(event) => event.key === "Enter" && next()} />
              </label>
              <label className="onboarding-label">
                <span>Pronouns <em>optional</em></span>
                <input value={pronouns} placeholder="e.g. he/him" onChange={(event) => setPronouns(event.target.value)} onKeyDown={(event) => event.key === "Enter" && next()} />
              </label>
              <div className="onboarding-hint"><UserRound size={15} /> Your profile will be saved as editable local Markdown.</div>
            </div>
          ) : null}

          {step === 1 ? (
            <div className="onboarding-fields onboarding-fields-enter">
              <label className="onboarding-label">
                <span>One thing you want to make progress on</span>
                <div className="onboarding-goal-input">
                  <input autoFocus value={goalInput} placeholder="Ship my portfolio, learn Rust…" onChange={(event) => setGoalInput(event.target.value)} onKeyDown={handleGoalKeyDown} />
                  <button type="button" onClick={addGoal} aria-label="Add goal"><CirclePlus size={19} /></button>
                </div>
              </label>
              <div className="onboarding-goals" aria-live="polite">
                {goals.map((goal) => <span key={goal}>{goal}<button type="button" onClick={() => setGoals((items) => items.filter((item) => item !== goal))} aria-label={`Remove ${goal}`}><X size={13} /></button></span>)}
                {!goals.length ? <div className="onboarding-goal-empty"><Compass size={17} /> Goals help Ares keep the right things in view.</div> : null}
              </div>
              <div className="onboarding-choice-row">
                <span>How do you prefer to work?</span>
                <div>{WORK_STYLES.map((style) => <button key={style} type="button" className={codingStyle === style ? "selected" : ""} onClick={() => setCodingStyle(style)}>{style}</button>)}</div>
              </div>
            </div>
          ) : null}

          {step === 2 ? (
            <div className="onboarding-fields onboarding-fields-enter onboarding-final-fields">
              <label className="onboarding-label">
                <span>Model</span>
                <select value={model} onChange={(event) => setModel(event.target.value)}>
                  {modelGroups.map((group) => <optgroup key={group.label} label={group.label}>{group.models.map((item) => <option key={item.id} value={item.id}>{item.label} — {item.provider}</option>)}</optgroup>)}
                </select>
              </label>
              <div className="onboarding-choice-row personality">
                <span>How should Ares show up?</span>
                <div>{PERSONALITIES.map((item) => <button key={item.id} type="button" className={personality === item.id ? "selected" : ""} onClick={() => setPersonality(item.id)}><strong>{item.title}</strong><small>{item.copy}</small></button>)}</div>
              </div>
              <label className="onboarding-label onboarding-style-select">
                <span>Response style</span>
                <select value={assistantStyle} onChange={(event) => setAssistantStyle(event.target.value)}>{ASSISTANT_STYLES.map((style) => <option key={style} value={style}>{style}</option>)}</select>
              </label>
            </div>
          ) : null}

          {error ? <p className="onboarding-error" role="alert">{error}</p> : null}
          <footer className="onboarding-actions">
            {step > 0 ? <button type="button" className="onboarding-back" onClick={() => { setError(""); setStep((current) => current - 1); }}><ArrowLeft size={16} /> Back</button> : <span />}
            {step < STEPS.length - 1 ? <button type="button" className="onboarding-primary" onClick={next}>Continue <ArrowRight size={16} /></button> : <button type="button" className="onboarding-primary" disabled={submitting} onClick={finish}>{submitting ? "Saving your space…" : "Meet Ares"} {!submitting && <Sparkles size={16} />}</button>}
          </footer>
        </section>
      </section>
    </main>
  );
}
