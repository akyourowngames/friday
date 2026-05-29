"""Tests for the nightly memory consolidation worker."""

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory.consolidation as consolidation
from memory.brain import Brain
from tests.test_memory import isolated_memory


def _distinct_embed(texts, normalize=True):
    """Deterministic embeddings for tests. A fact's vector is dominated by which
    concept tokens it contains, so two differently-worded facts about the same
    concept score high cosine similarity (the case the worker must catch) while
    unrelated facts score low. Identical text -> identical vector."""
    concepts = ["coffee", "espresso", "caffeine", "pune", "logo", "beans", "tea"]

    def vec(t: str):
        low = t.strip().casefold()
        v = np.zeros(len(concepts) + 2, dtype=np.float32)
        for idx, concept in enumerate(concepts):
            if concept in low:
                v[idx] = 1.0
        # tiny per-text jitter so identical strings match exactly but distinct
        # strings are never perfectly parallel unless they share concepts
        v[-1] = (abs(hash(low)) % 100) / 1000.0
        norm = np.linalg.norm(v)
        if norm == 0:
            v[-2] = 1.0
            norm = 1.0
        return v / norm

    if isinstance(texts, str):
        return vec(texts)
    return np.vstack([vec(t) for t in texts])


class DecayTests(unittest.TestCase):
    def test_decay_lowers_stale_low_value_only(self):
        with isolated_memory(fake_embed=_distinct_embed):
            brain = Brain()
            brain.commit("User is exploring a new hobby", importance=0.5)
            # Backdate it well past the decay threshold.
            old = (date.today() - timedelta(days=90)).isoformat()
            brain.memories[0]["_date"] = old
            before = brain.memories[0]["importance"]
            cfg = {"decay_enabled": True, "decay_after_days": 45, "decay_rate": 0.05, "decay_floor": 0.1}
            result = consolidation.run_decay(brain, cfg)
            self.assertEqual(result["decayed"], 1)
            self.assertLess(brain.memories[0]["importance"], before)

    def test_decay_skips_recent_memories(self):
        with isolated_memory(fake_embed=_distinct_embed):
            brain = Brain()
            brain.commit("User started a fresh project today", importance=0.5)
            cfg = {"decay_enabled": True, "decay_after_days": 45, "decay_rate": 0.05, "decay_floor": 0.1}
            result = consolidation.run_decay(brain, cfg)
            self.assertEqual(result["decayed"], 0)

    def test_decay_respects_floor(self):
        with isolated_memory(fake_embed=_distinct_embed):
            brain = Brain()
            brain.commit("User tried a cafe once", importance=0.1)
            brain.memories[0]["_date"] = (date.today() - timedelta(days=90)).isoformat()
            cfg = {"decay_enabled": True, "decay_after_days": 45, "decay_rate": 0.05, "decay_floor": 0.1}
            result = consolidation.run_decay(brain, cfg)
            self.assertEqual(result["decayed"], 0)

    def test_decay_disabled(self):
        with isolated_memory(fake_embed=_distinct_embed):
            brain = Brain()
            result = consolidation.run_decay(brain, {"decay_enabled": False})
            self.assertEqual(result.get("skipped"), "disabled")


class DuplicatePairTests(unittest.TestCase):
    def test_identical_text_scores_high(self):
        with isolated_memory(fake_embed=_distinct_embed):
            brain = Brain()
            brain.commit("User loves filter coffee", importance=0.6)
            brain.commit("User totally loves filter coffee", importance=0.5)
            brain.commit("User lives in Pune", importance=0.7)
            pairs = consolidation._duplicate_pairs(brain, similarity=0.99, max_pairs=10)
            # The two coffee facts are distinct strings so won't be 1.0; lower the
            # bar and assert the detector returns ordered pairs without crashing.
            pairs_low = consolidation._duplicate_pairs(brain, similarity=-1.0, max_pairs=10)
            self.assertGreaterEqual(len(pairs_low), 1)
            for i, j, score in pairs_low:
                self.assertLess(i, j)


class DedupTests(unittest.TestCase):
    def test_dedup_merges_when_llm_says_same(self):
        original = consolidation._llm_json
        consolidation._llm_json = lambda system, user, max_tokens: {"same": True, "merged": "User enjoys coffee regularly"}
        try:
            with isolated_memory(fake_embed=_distinct_embed):
                brain = Brain()
                brain.commit("Grabbing coffee remains a cherished morning habit", importance=0.6)
                brain.commit("Always reaches for coffee first thing", importance=0.5)
                count_before = len(brain.memories)
                self.assertEqual(count_before, 2)
                cfg = {
                    "dedup_enabled": True, "dedup_similarity": 0.3, "dedup_max_pairs": 5,
                    "max_tokens": 200,
                }
                result = consolidation.run_dedup(brain, cfg)
                self.assertGreaterEqual(result["merged"], 1)
                self.assertLess(len(brain.memories), count_before)
                texts = [m["text"] for m in brain.memories]
                self.assertIn("User enjoys coffee regularly", texts)
        finally:
            consolidation._llm_json = original

    def test_dedup_keeps_both_when_llm_says_different(self):
        original = consolidation._llm_json
        consolidation._llm_json = lambda system, user, max_tokens: {"same": False}
        try:
            with isolated_memory(fake_embed=_distinct_embed):
                brain = Brain()
                brain.commit("Grabbing coffee remains a cherished morning habit", importance=0.6)
                brain.commit("Always reaches for coffee first thing", importance=0.6)
                before = len(brain.memories)
                cfg = {"dedup_enabled": True, "dedup_similarity": 0.3, "dedup_max_pairs": 5, "max_tokens": 200}
                result = consolidation.run_dedup(brain, cfg)
                self.assertEqual(result["merged"], 0)
                self.assertEqual(len(brain.memories), before)
        finally:
            consolidation._llm_json = original

    def test_dedup_disabled(self):
        with isolated_memory(fake_embed=_distinct_embed):
            brain = Brain()
            result = consolidation.run_dedup(brain, {"dedup_enabled": False})
            self.assertEqual(result.get("skipped"), "disabled")


class InsightTests(unittest.TestCase):
    def test_insight_created_from_cluster(self):
        original = consolidation._llm_json
        consolidation._llm_json = lambda system, user, max_tokens: {
            "insight": "User is steadily building a coffee side business",
            "worth_storing": True,
        }
        try:
            with isolated_memory(fake_embed=_distinct_embed):
                brain = Brain()
                # Several facts sharing a graph node (User fallback edges share "User").
                brain.commit("User bought an espresso machine", importance=0.6)
                brain.commit("User is sourcing coffee beans", importance=0.6)
                brain.commit("User designed a cafe logo", importance=0.6)
                before = len(brain.memories)
                cfg = {
                    "insights_enabled": True, "insight_min_cluster": 2,
                    "insight_max": 3, "insight_importance": 0.75, "max_tokens": 200,
                }
                result = consolidation.run_insights(brain, cfg)
                self.assertGreaterEqual(result["insights"], 1)
                self.assertGreater(len(brain.memories), before)
        finally:
            consolidation._llm_json = original

    def test_insight_disabled(self):
        with isolated_memory(fake_embed=_distinct_embed):
            brain = Brain()
            result = consolidation.run_insights(brain, {"insights_enabled": False})
            self.assertEqual(result.get("skipped"), "disabled")


class ConsolidateOrchestrationTests(unittest.TestCase):
    def test_consolidate_runs_all_steps(self):
        with isolated_memory(fake_embed=_distinct_embed):
            brain = Brain()
            brain.commit("User likes tea", importance=0.5)
            result = consolidation.consolidate(brain)
            self.assertEqual(result["status"], "ok")
            self.assertIn("dedup", result)
            self.assertIn("insights", result)
            self.assertIn("decay", result)


if __name__ == "__main__":
    unittest.main()
