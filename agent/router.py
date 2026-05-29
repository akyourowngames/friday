"""Markdown-gated tool router.

The routing decision is:
1. Embed category examples from `tools/TOOL_ROUTING_CATEGORIES.md`.
2. Pick one markdown-owned category.
3. Embed utterances from `tools/TOOL_UTTERANCES.md`.
4. Pick one exact tool inside the chosen category.

No keyword shortcuts and no full-registry fishing expedition. Collisions are
fixed by editing the markdown category or utterance files, not by adding code.
"""

import json
from pathlib import Path

import numpy as np

from config import settings
from tools.registry import get_tools, get_tool
from .embedder import embed

_CACHE_DIR = Path(settings.storage_dir)
_TOOL_CACHE = _CACHE_DIR / "tool_embeddings.npy"
_TOOL_TEXTS_CACHE = _CACHE_DIR / "tool_texts.json"
_TOOL_OWNERS_CACHE = _CACHE_DIR / "tool_owners.json"
_CATEGORY_CACHE = _CACHE_DIR / "tool_category_embeddings.npy"
_CATEGORY_TEXTS_CACHE = _CACHE_DIR / "tool_category_texts.json"
_CATEGORY_OWNERS_CACHE = _CACHE_DIR / "tool_category_owners.json"
ROUTING_POLICY_PATH = Path(__file__).resolve().parent.parent / "routing_policy.md"
_UTTERANCES_PATH = Path(__file__).resolve().parent.parent / settings.tool_utterances_file
_CATEGORIES_PATH = Path(__file__).resolve().parent.parent / settings.tool_routing_categories_file


# ─── Exported helpers (consumed by core.py and tests) ───────────────────────

def _load_small_talk_text() -> str:
    """Load the routing contrast text from routing_policy.md."""
    if ROUTING_POLICY_PATH.exists():
        no_memory_small_talk = _load_routing_section("No Memory Small Talk Text")
        text = f"Routing Contrast Text. No Memory Small Talk Text: {no_memory_small_talk}".strip()
        if text:
            return text
    return (
        "Conversational turn with no request for external data, local action, "
        "memory access, file access, search, playback, generation, or side effects."
    )


def _load_capability_rules() -> list[dict]:
    """Load capability rules from tools.capabilities (kept for test imports)."""
    try:
        from tools.capabilities import build_capability_rules
        return build_capability_rules()
    except Exception:
        return []


def _parse_file_generating_tools():
    if not settings.file_generating_tools:
        return set()
    return {t.strip() for t in settings.file_generating_tools.split(",") if t.strip()}

_FILE_GENERATING_TOOLS = _parse_file_generating_tools()


# ─── Internal helpers ───────────────────────────────────────────────────────

def _load_routing_section(heading: str) -> str:
    if not ROUTING_POLICY_PATH.exists():
        return ""
    targets = {f"# {heading}".casefold(), f"## {heading}".casefold()}
    lines = []
    in_section = False
    for raw_line in ROUTING_POLICY_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("# ") or line.startswith("## "):
            in_section = line.casefold() in targets
            continue
        if in_section and line:
            lines.append(line)
    return " ".join(lines).strip()


def _load_tool_utterances() -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Parse the utterance bank from TOOL_UTTERANCES.md.

    Returns:
        (utterances_by_tool, slug_hints_by_phrase)

    slug_hints_by_phrase maps a phrase to {"tool_slug": "SLUG"} when the
    phrase line contains a `[slug:SLUG]` annotation. This lets composio
    utterances carry their resolved slug without a separate capability layer.
    """
    if not _UTTERANCES_PATH.exists():
        return {}, {}
    utterances: dict[str, list[str]] = {}
    slug_hints: dict[str, dict] = {}
    current: str | None = None
    for raw_line in _UTTERANCES_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            head = stripped[3:].strip()
            current = head if head else None
            continue
        if current and stripped.startswith("- "):
            phrase = stripped[2:].strip()
            if not phrase:
                continue
            phrase, hint = _clean_utterance_annotations(phrase)
            if hint and phrase:
                slug_hints[phrase] = hint
            if phrase:
                utterances.setdefault(current, []).append(phrase)
    return utterances, slug_hints


def _clean_utterance_annotations(phrase: str) -> tuple[str, dict]:
    hint: dict = {}
    clean = str(phrase or "").strip()
    while True:
        start = clean.find("[")
        if start < 0:
            break
        end = clean.find("]", start + 1)
        if end < 0:
            break
        tag = clean[start + 1:end].strip()
        before = clean[:start].strip()
        after = clean[end + 1:].strip()
        if tag.startswith("slug:"):
            slug = tag[5:].strip()
            if slug:
                hint["tool_slug"] = slug
            clean = f"{before} {after}".strip()
            continue
        if tag.startswith("args:"):
            raw_args = tag[5:].strip()
            args = dict(hint.get("args") or {})
            for part in raw_args.split(","):
                key, found, value = part.partition("=")
                if found and key.strip() and value.strip():
                    args[key.strip()] = value.strip()
            if args:
                hint["args"] = args
            clean = f"{before} {after}".strip()
            continue
        if tag.casefold() == "direct":
            hint["direct"] = True
            clean = f"{before} {after}".strip()
            continue
        break
    return clean, hint


def _load_tool_categories() -> list[dict]:
    if not _CATEGORIES_PATH.exists():
        return []

    categories = []
    current: dict | None = None
    in_categories = False
    for raw_line in _CATEGORIES_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_categories = line[3:].strip().casefold() == "categories"
            current = None
            continue
        if not in_categories:
            continue
        if line.startswith("### "):
            name = line[4:].strip()
            current = {"name": name, "tools": [], "texts": []}
            if name:
                categories.append(current)
            continue
        if current is None or not line.startswith("- "):
            continue
        item = line[2:].strip()
        key, found, value = item.partition(":")
        if found and key.strip().casefold() == "tools":
            current["tools"] = [part.strip() for part in value.split(",") if part.strip()]
            continue
        if item:
            current["texts"].append(item)
    return categories


# ─── Router ─────────────────────────────────────────────────────────────────

class ToolRouter:
    def __init__(self, top_k=None):
        self.top_k = top_k or settings.tool_top_k
        self.threshold = settings.tool_similarity_threshold
        # Kept for backward-compat with one test that stubs these directly.
        self.winner_margin = 0.0
        self._tool_embeddings = None
        self._tool_names: list[str] = []
        self._tool_texts: list[str] = []
        self._row_owner_idx: list[int] = []
        self._slug_hints: dict[str, dict] = {}
        self._categories: list[dict] = []
        self._category_names: list[str] = []
        self._category_texts: list[str] = []
        self._category_owner_idx: list[int] = []
        self._category_embeddings = None
        self._small_talk_emb = None
        self._last_capability_hint: dict[str, dict] = {}
        self._last_generated_file = None
        self._last_decision: dict = {
            "query": "",
            "category": "",
            "selected": [],
            "scores": [],
            "reason": "not_run",
        }
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _embed_tools(self):
        """Build the utterance embedding index from the MD file."""
        tools = get_tools()
        self._tool_names = [t["name"] for t in tools]
        if not self._tool_names:
            self._tool_texts = []
            self._row_owner_idx = []
            self._tool_embeddings = np.array([])
            return

        utterance_bank, self._slug_hints = _load_tool_utterances()

        row_texts: list[str] = []
        row_owners: list[int] = []
        for owner_idx, t in enumerate(tools):
            phrases = utterance_bank.get(t["name"]) or []
            if phrases:
                for phrase in phrases:
                    row_texts.append(phrase)
                    row_owners.append(owner_idx)
            else:
                # Fallback: use description + examples for tools not yet in the bank.
                fallback = f"{t['name']}: {t['description']}"
                if t.get("examples"):
                    fallback += " | " + " | ".join(t["examples"])
                row_texts.append(fallback)
                row_owners.append(owner_idx)

        self._tool_texts = row_texts
        self._row_owner_idx = row_owners

        # Cache check: skip re-embedding if texts + owners unchanged.
        cached_texts = None
        if _TOOL_TEXTS_CACHE.exists():
            try:
                cached_texts = json.loads(_TOOL_TEXTS_CACHE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_texts = None
        cached_owners = None
        if _TOOL_OWNERS_CACHE.exists():
            try:
                cached_owners = json.loads(_TOOL_OWNERS_CACHE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_owners = None
        owner_names = [self._tool_names[i] for i in row_owners]

        if (
            cached_texts == row_texts
            and cached_owners == owner_names
            and _TOOL_CACHE.exists()
        ):
            self._tool_embeddings = np.load(_TOOL_CACHE)
            return

        self._tool_embeddings = embed(row_texts)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.save(_TOOL_CACHE, self._tool_embeddings)
        _TOOL_TEXTS_CACHE.write_text(
            json.dumps(row_texts, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _TOOL_OWNERS_CACHE.write_text(
            json.dumps(owner_names, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _embed_categories(self):
        """Build the category embedding index from the MD file."""
        self._categories = _load_tool_categories()
        self._category_names = [item["name"] for item in self._categories]
        if not self._category_names:
            self._category_texts = []
            self._category_owner_idx = []
            self._category_embeddings = np.array([])
            return

        row_texts = []
        row_owners = []
        for owner_idx, category in enumerate(self._categories):
            texts = list(category.get("texts") or [])
            if not texts:
                texts = [category.get("name", "")]
            for text in texts:
                clean = str(text or "").strip()
                if clean:
                    row_texts.append(clean)
                    row_owners.append(owner_idx)

        self._category_texts = row_texts
        self._category_owner_idx = row_owners
        owner_names = [self._category_names[i] for i in row_owners]

        cached_texts = None
        if _CATEGORY_TEXTS_CACHE.exists():
            try:
                cached_texts = json.loads(_CATEGORY_TEXTS_CACHE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_texts = None
        cached_owners = None
        if _CATEGORY_OWNERS_CACHE.exists():
            try:
                cached_owners = json.loads(_CATEGORY_OWNERS_CACHE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_owners = None

        if (
            cached_texts == row_texts
            and cached_owners == owner_names
            and _CATEGORY_CACHE.exists()
        ):
            self._category_embeddings = np.load(_CATEGORY_CACHE)
            return

        self._category_embeddings = embed(row_texts) if row_texts else np.array([])
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.save(_CATEGORY_CACHE, self._category_embeddings)
        _CATEGORY_TEXTS_CACHE.write_text(
            json.dumps(row_texts, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _CATEGORY_OWNERS_CACHE.write_text(
            json.dumps(owner_names, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _per_tool_scores(self, row_similarities: np.ndarray) -> tuple[np.ndarray, list[int]]:
        """Max-pool row similarities to per-tool scores.

        Returns (per_tool_scores, per_tool_best_row_idx).
        """
        owners = self._row_owner_idx
        if not owners:
            # Backward-compat: tests may stub _tool_embeddings with one row per
            # tool and not populate _row_owner_idx.
            return row_similarities, list(range(len(row_similarities)))
        n_tools = len(self._tool_names)
        per_tool = np.full(n_tools, -np.inf, dtype=row_similarities.dtype)
        per_tool_row = [-1] * n_tools
        for row_idx, owner in enumerate(owners):
            score = float(row_similarities[row_idx])
            if score > per_tool[owner]:
                per_tool[owner] = score
                per_tool_row[owner] = row_idx
        return per_tool, per_tool_row

    def _per_category_scores(self, row_similarities: np.ndarray) -> tuple[np.ndarray, list[int]]:
        owners = self._category_owner_idx
        if not owners:
            return row_similarities, list(range(len(row_similarities)))
        n_categories = len(self._category_names)
        per_category = np.full(n_categories, -np.inf, dtype=row_similarities.dtype)
        per_category_row = [-1] * n_categories
        for row_idx, owner in enumerate(owners):
            score = float(row_similarities[row_idx])
            if score > per_category[owner]:
                per_category[owner] = score
                per_category_row[owner] = row_idx
        return per_category, per_category_row

    def _selected_category(self, q_emb) -> tuple[dict | None, list[dict], str]:
        if self._category_embeddings is None:
            self._embed_categories()
        if not self._category_names or self._category_embeddings is None or len(self._category_embeddings) == 0:
            return None, [], "no_category_index"
        category_embeddings = np.asarray(self._category_embeddings)
        query_embedding = np.asarray(q_emb)
        if (
            category_embeddings.ndim != 2
            or query_embedding.ndim != 1
            or category_embeddings.shape[1] != query_embedding.shape[0]
        ):
            return None, [], "embedding_dimension_mismatch"

        row_sims = np.dot(category_embeddings, query_embedding)
        per_category_sims, per_category_rows = self._per_category_scores(row_sims)
        best_idx = int(np.argmax(per_category_sims))
        best_score = float(per_category_sims[best_idx])
        scores = [
            {"category": self._category_names[int(idx)], "score": round(float(per_category_sims[int(idx)]), 4)}
            for idx in np.argsort(per_category_sims)[::-1][: min(5, len(per_category_sims))]
        ]
        if best_score < settings.tool_category_threshold:
            return None, scores, "below_category_threshold"
        category = dict(self._categories[best_idx])
        best_row = per_category_rows[best_idx]
        if 0 <= best_row < len(self._category_texts):
            category["matched_text"] = self._category_texts[best_row]
        category["score"] = round(best_score, 4)
        return category, scores, "selected"

    def select_tools(self, query, q_emb=None):
        """Select one exact tool through markdown category then utterance gates."""
        self._last_capability_hint = {}

        if len(query.strip()) < settings.embedding_min_chars:
            self._last_decision = {
                "query": query,
                "category": "",
                "selected": [],
                "scores": [],
                "reason": "below_embedding_min_chars",
            }
            return []

        if self._tool_embeddings is None or len(get_tools()) != len(self._tool_names):
            self._embed_tools()

        if not self._tool_names:
            self._last_decision = {
                "query": query,
                "category": "",
                "selected": [],
                "scores": [],
                "reason": "no_registered_tools",
            }
            return []

        if q_emb is None:
            q_emb = embed(query)

        category, category_scores, category_reason = self._selected_category(q_emb)
        if category is None:
            self._last_decision = {
                "query": query,
                "category": "",
                "selected": [],
                "scores": [],
                "category_scores": category_scores,
                "reason": category_reason,
            }
            return []

        allowed_names = {
            name
            for name in category.get("tools", [])
            if name in self._tool_names
        }
        if not allowed_names:
            self._last_decision = {
                "query": query,
                "category": category.get("name", ""),
                "selected": [],
                "scores": [],
                "category_scores": category_scores,
                "reason": "category_has_no_registered_tools",
            }
            return []

        # Score utterance rows, then collapse to per-tool max inside the chosen category.
        tool_embeddings = np.asarray(self._tool_embeddings)
        query_embedding = np.asarray(q_emb)
        if (
            tool_embeddings.ndim != 2
            or query_embedding.ndim != 1
            or tool_embeddings.shape[1] != query_embedding.shape[0]
        ):
            self._last_decision = {
                "query": query,
                "category": category.get("name", ""),
                "selected": [],
                "scores": [],
                "category_scores": category_scores,
                "reason": "embedding_dimension_mismatch",
            }
            return []
        row_sims = np.dot(tool_embeddings, query_embedding)
        per_tool_sims, per_tool_best_row = self._per_tool_scores(row_sims)

        best_idx = None
        best_score = -float("inf")
        category_tool_scores = []
        for idx, tool_name in enumerate(self._tool_names):
            if tool_name not in allowed_names:
                continue
            score = float(per_tool_sims[idx])
            category_tool_scores.append({"tool": tool_name, "score": round(score, 4)})
            if score > best_score:
                best_score = score
                best_idx = idx
        category_tool_scores.sort(key=lambda item: item["score"], reverse=True)
        scores = category_tool_scores[: min(5, len(category_tool_scores))]

        if best_score < self.threshold:
            self._last_decision = {
                "query": query,
                "category": category.get("name", ""),
                "selected": [],
                "scores": scores,
                "category_scores": category_scores,
                "reason": "below_tool_threshold",
            }
            return []

        selected = []
        if best_idx is not None:
            tool_name = self._tool_names[int(best_idx)]
            tool_obj = get_tool(tool_name)
            if tool_obj:
                selected.append(tool_obj)
                best_row = per_tool_best_row[int(best_idx)]
                if 0 <= best_row < len(self._tool_texts):
                    phrase = self._tool_texts[best_row]
                    hint = self._slug_hints.get(phrase)
                    if hint:
                        args = dict(hint.get("args") or {})
                        if hint.get("tool_slug"):
                            args["action"] = "execute"
                            args["tool_slug"] = hint["tool_slug"]
                        self._last_capability_hint[tool_name] = {
                            "args": args,
                            "direct": bool(hint.get("direct")),
                            "score": round(float(per_tool_sims[best_idx]), 4),
                        }

        self._last_decision = {
            "query": query,
            "category": category.get("name", ""),
            "category_match": category.get("matched_text", ""),
            "selected": [t["name"] for t in selected],
            "scores": scores,
            "category_scores": category_scores,
            "reason": "selected" if selected else "below_tool_threshold",
        }
        return selected

    def capability_hint(self, tool_name: str) -> dict:
        """Return slug hint for a tool from the most recent selection."""
        return dict(self._last_capability_hint.get(tool_name, {}))

    def last_decision(self) -> dict:
        return dict(self._last_decision)
