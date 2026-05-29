# KING Cognition Design

JARVIS-level design write-up that the `cognition/` substrate implements against.
This is the "why and what" companion to `todo.md` (the build plan) and the
`cognition/` code (the "how"). Everything here respects the project rules: no
regex, no keyword routing, no hardcoded phrases, config/markdown-driven,
local-first, verified by structured results.

---

## 1. Vision

What separates JARVIS from a normal assistant is **continuity of attention**. He
is already mid-thought when you arrive, having watched and modeled while you were
away. KING should feel like a system that:

- Was already thinking about you before you spoke.
- Holds a model of your life as a moving object, not a fact list (it knows the
  slope, not just the current value).
- Has opinions and the restraint to mostly keep them.
- Treats memory as narrative (arcs), not rows.
- Earns silence. Quiet 95% of the time, devastatingly relevant the other 5%.

The emotional target: an occasional small "how did it know that," immediately
followed by "oh — because it actually understood." The chill comes from
inference over accumulated signal, never a scripted trick.

## 2. Missing capabilities (before this work)

1. A self-model of the user over time (trajectory, not snapshot).
2. An idle cognition loop (JARVIS is mostly idle compute).
3. Episodic narrative (stories, not fragments).
4. Situational gating (is now a good moment to speak).
5. Affective/emotional memory.
6. Outcome learning / trust calibration.
7. A blackboard / shared cognitive state.
8. Intent simulation before output.

## 3. Ten god-tier features

1. **Life Cadence Engine** — per-node 24x7 rhythm; deviation = "you usually do
   this now and haven't." *(Shipped: `cognition/cadence.py`.)*
2. **Belief Revision Ledger** — keep the change, not just the new value; detect
   drift direction. *(Pending — Phase 2.)*
3. **Anticipatory Pre-Fetch** — warm cache of likely next queries during idle.
4. **Episode Stitching** — cluster memories into narrative episodes.
   *(Shipped: `cognition/episodes.py`.)*
5. **Intent Simulation Pass** — theory-of-mind pre-output conditioning.
6. **Self-Reflection Loop** — fill the graph `reflections`/`procedures` from
   corrections; stop repeating mistakes. *(Pending — Phase 2.)*
7. **Situational Awareness Layer** — fuse ambient signals into a speak/stay-quiet
   gate. *(Shipped: `cognition/situation.py`.)*
8. **Social Memory & Open-Loop Tracker** — manage people and verbal commitments.
9. **Affective Memory Tier** — valence/arousal per memory as a recall signal.
   *(Pending — Phase 2.)*
10. **Decision Journal & Trust Calibration** — learn whether advice was good;
    earn assertiveness per domain. *(Pending — Phase 2.)*

Each feature is built from existing primitives (graph edges with tiers +
temporal supersede, `recall_unified()`, the embedding router, the maintenance
scheduler, the unused `reflections`/`procedures` graph lists, the Obsidian
projection).

## 4. Proactive intelligence system

Pipeline every candidate passes through:

```
Signal -> Trigger -> Relevance -> Situational Gate -> Confidence
       -> Attention Budget (daily cap + novelty) -> Speak
```

- **Timing**: candidates are generated during idle maintenance ticks, delivered
  only at natural boundaries (session start, after a silence gap, when the user
  surfaces from a busy period). Never mid-flow.
- **Triggers** are scored candidates in a queue, not message-firing events.
- **Scoring**: `relevance · freshness · importance · (1-annoyance) · fit`.
- **Adaptive threshold**: rises right after KING speaks, decays back over time —
  the single most important anti-annoyance mechanism.
- **Memory signals**: reward cross-reference candidates (two distant memories
  about the same node) over single-source ones.
- **Attention engine**: strict daily budget + novelty dedup (cosine).
- **Notification psychology**: earn it with specificity, one thought at a time,
  always give a low-friction exit.
- **When NOT to interrupt**: high load, low availability, recent dismissal,
  threshold not cleared, duplicate novelty. Default is silence.

Implemented in `cognition/proactive.py` (scoring, adaptive threshold, budget,
novelty, dismissal penalty) gated by `cognition/situation.py`.

## 5. Human-like relationship engine

The trap is performed warmth. Real rapport is earned, specific, slightly scarce.

- **Relationship modeling**: learned per-user state (formality, humor tolerance,
  directness, trust scalar) — never fixed.
- **Boundaries**: detected by embedding contrast, stored as graph edges, treated
  as hard gates that override relevance.
- **Contextual humor**: only when trust is high, load is low, and there is a
  genuine callback to shared memory.
- **Memory warmth**: referencing the past unprompted and accurately.
- **Adaptive tone**: drifts slowly toward what the user responds well to.
- **Trust calibration**: hedge early; earn directness per domain (§10).

Meta-rule (enforced via `persona.md` + `CHAT_POLISH_POLICY.md`): never claim a
feeling it does not have. Warmth must be referential, not emotional.

## 6. Cognitive architecture

A blackboard (shared working memory) + event bus, not a pipeline. The
`folder_watcher/bus.py` pattern is the template for a brain-wide bus. Modules
read/write a shared working memory and emit events; they do not call each other
directly, so they can be built one at a time and degrade gracefully.

Module map: Memory Manager = `Brain`; Reasoner = the NIM call; Planner (thin LLM
planner); Reflector (§6); World Model = Cadence + Situation; Goal Tracker = goal
edges + drift; Curiosity Engine (graph gaps); Contradiction Detector (temporal
supersede + ledger); Self-Correction (Verifier + Reflector); Prediction Engine
(pre-fetch + outcome); Intent Simulator (§5); Personality (`persona.md` + tone
vector).

## 7. Memory beyond RAG

Memory types are tiers + signals; retrieval is multi-signal fusion (the existing
`recall_unified()` already fuses semantic rank + edge rank + one-hop expansion).
Add recency, affect congruence, and episode coherence as additional signals; all
weights live in config. Make decay tier-aware (emotional/identity slowest).
Memory resurrection: a low-weight pass over the archive resurfaces a forgotten
memory that strongly out-scores peers for the current query.

Full ranking target:

```
score(m) = w_sem·cos + w_edge·edge + w_hop·hop + w_rec·recency
         + w_imp·importance + w_aff·affect + w_epi·episode - w_dup·redundancy
```

## 8. Twenty surprise ideas

Dream consolidation; semantic déjà vu; question debt; confidence-tagged answers;
mood-aware verbosity; earned "what changed while away" briefing; self-doubt
self-correction events; relationship temperature; effort estimation;
counterfactual memory; embedding-drift personality mirror; silent watchlist;
energy-aware scheduling; memory provenance on demand; gradual goal archaeology;
affective bookmarks; rubber-duck mode; cross-domain pattern transfer;
trust-gated autonomy; the honesty ledger.

## 9. Build order (highest ROI for a solo dev)

- **Phase 0 (shipped)**: episode stitching + cadence + situation + proactive
  engine. Highest intelligence-per-line; all additive maintenance steps.
- **Phase 1**: live signal feeds + LLM phrasing + delivery (delivery needs core
  authority).
- **Phase 2**: belief drift, affect tier, reflection loop, resurrection,
  decision journal.
- **Delay**: the full blackboard refactor and Planner; simulate inside the
  existing loop for now. Do not refactor `core.py` for this.

Cheap alternatives: persona + tone vector + reflections instead of fine-tuning;
the existing numpy/FAISS store instead of a new vector DB; arithmetic candidate
generation with one LLM call only to phrase the winner; affect piggybacked on
the existing per-turn LLM call.

## 10. Brutal weakness audit

1. The whole stack rides on one embedding model — correlated failure near
   thresholds. Add a cheap LLM tie-breaker only in the ambiguous band.
2. Memory is a fact-bag pretending to be a brain until episodes exist.
3. The Verifier is a single cosine threshold — too blunt to separate "grounded"
   from "topically similar."
4. No idle life means no real anticipation; "proactive" collapses to scripted.
5. Storing change while discarding the meaning of change (the trajectory).
6. Performed emotion is what would secretly make it feel fake. Warmth must be
   referential.
7. "No hardcoding" pushed complexity into invisible numeric magic (margins,
   thresholds). The honest version: thresholds learned/adaptive from outcomes.
8. Assistants feel dumb because they lack a model of the user as a changing
   system, session continuity, timing, and stakes. The gap KING must close is
   temporal modeling + idle cognition + narrative recall — which Phase 0 starts.
