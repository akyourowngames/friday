import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory.brain as brain_mod
from memory.brain import Brain
from tests.test_memory import isolated_memory


def _ones_embed(texts, normalize=True):
    if isinstance(texts, str):
        return np.ones(4, dtype=np.float32)
    return np.ones((len(texts), 4), dtype=np.float32)


class MemoryGDTierTests(unittest.TestCase):
    def test_empty_brain_reports_gd_tier(self):
        with isolated_memory():
            brain = Brain()
            tier = brain.tier_report()
            self.assertEqual(tier["tier"], "gd")
            self.assertTrue(tier["integrity_ok"])

    def test_verify_integrity_after_commit(self):
        with isolated_memory(fake_embed=_ones_embed):
            brain = Brain()
            self.assertTrue(brain.commit("User lives in Delhi", importance=0.8))
            integrity = brain.verify_integrity()
            self.assertTrue(integrity["ok"])

    def test_query_cache_reuses_embedding(self):
        calls = {"count": 0}

        def counting_embed(texts, normalize=True):
            calls["count"] += 1
            return _ones_embed(texts, normalize=normalize)

        with isolated_memory(fake_embed=counting_embed):
            brain = Brain()
            query = "herbal tea preference for mornings"
            brain._embed_query(query)
            after_first = calls["count"]
            self.assertEqual(after_first, 1)
            brain._embed_query(query)
            self.assertEqual(calls["count"], after_first)
            self.assertEqual(len(brain._query_cache), 1)

    def test_maintain_rebuilds_when_integrity_fails(self):
        with isolated_memory(fake_embed=_ones_embed) as (memory_dir, backup_dir):
            brain = Brain()
            self.assertTrue(brain.commit("User works remotely", importance=0.6))
            brain._embeddings = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
            report = brain.maintain(rebuild=False, backup=True)
            self.assertTrue(report["rebuilt"])
            self.assertEqual(report["after"]["tier"], "gd")
            self.assertTrue(backup_dir.exists())

    def test_chunked_rebuild_handles_many_entries(self):
        with isolated_memory(fake_embed=_ones_embed, max_entries=500):
            brain_mod.settings.memory_rebuild_batch_size = 2
            try:
                brain = Brain()
                for idx in range(5):
                    self.assertTrue(brain.commit(f"User fact number {idx}", importance=0.5))
                self.assertEqual(brain._embeddings.shape[0], 5)
                self.assertEqual(brain._embeddings.shape[1], 4)
            finally:
                brain_mod.settings.memory_rebuild_batch_size = 64

    def test_assessment_includes_tier_and_integrity(self):
        with isolated_memory():
            brain = Brain()
            assessment = brain.system_assessment()
            self.assertIn("tier", assessment)
            self.assertIn("integrity", assessment)
            self.assertEqual(assessment["schema_version"], 3)


class MemoryOpsToolTests(unittest.TestCase):
    def test_memory_assess_structured(self):
        from tools.memory_ops import memory_assess

        with isolated_memory():
            result = memory_assess(response_format="structured")
            self.assertEqual(result["result"]["tier"]["tier"], "gd")

    def test_memory_remember_and_recall_structured(self):
        from tools.memory_ops import memory_recall, memory_remember

        with isolated_memory(fake_embed=_ones_embed):
            stored = memory_remember("User prefers dark mode UI", response_format="structured")
            self.assertTrue(stored["result"]["stored"])
            recalled = memory_recall("dark mode", response_format="structured")
            self.assertGreaterEqual(recalled["result"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
