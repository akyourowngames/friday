"""Focused regression tests for the KING memory system."""

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory.brain as brain_mod
from memory.brain import (
    Brain,
    _contradiction_category,
    _daily_date_from_memory_file,
    _is_vague,
    _load_policy_reject_phrases,
    _normalize_fact,
    _term_set,
)


def _ones_embed(texts, normalize=True):
    if isinstance(texts, str):
        return np.ones(4, dtype=np.float32)
    return np.ones((len(texts), 4), dtype=np.float32)


@contextmanager
def isolated_memory(fake_embed=_ones_embed, max_entries=2000, importance_min=0.0, importance_max=1.0):
    original = {
        "memory_dir": brain_mod.settings.memory_dir,
        "memory_backup_dir": brain_mod.settings.memory_backup_dir,
        "memory_index_file": brain_mod.settings.memory_index_file,
        "memory_embeddings_file": brain_mod.settings.memory_embeddings_file,
        "memory_archive_file": brain_mod.settings.memory_archive_file,
        "memory_graph_file": brain_mod.settings.memory_graph_file,
        "memory_graph_relations_file": brain_mod.settings.memory_graph_relations_file,
        "memory_max_entries": brain_mod.settings.memory_max_entries,
        "memory_importance_min": brain_mod.settings.memory_importance_min,
        "memory_importance_max": brain_mod.settings.memory_importance_max,
        "embed": brain_mod.embed,
        "MEMORY_DIR": brain_mod.MEMORY_DIR,
        "BACKUP_DIR": brain_mod.BACKUP_DIR,
        "MEMORY_GRAPH_RELATIONS_PATH": brain_mod.MEMORY_GRAPH_RELATIONS_PATH,
    }
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp) / "memories"
        backup_dir = Path(tmp) / "backups"
        brain_mod.settings.memory_dir = str(memory_dir)
        brain_mod.settings.memory_backup_dir = str(backup_dir)
        brain_mod.settings.memory_index_file = "memory_index.json"
        brain_mod.settings.memory_embeddings_file = "memory_embeddings.npy"
        brain_mod.settings.memory_archive_file = "memory_archive.jsonl"
        brain_mod.settings.memory_graph_file = "memory_graph.json"
        brain_mod.settings.memory_graph_relations_file = original["memory_graph_relations_file"]
        brain_mod.settings.memory_max_entries = max_entries
        brain_mod.settings.memory_importance_min = importance_min
        brain_mod.settings.memory_importance_max = importance_max
        brain_mod.MEMORY_DIR = Path(brain_mod.settings.memory_dir)
        brain_mod.BACKUP_DIR = Path(brain_mod.settings.memory_backup_dir)
        brain_mod.MEMORY_GRAPH_RELATIONS_PATH = Path(brain_mod.settings.memory_graph_relations_file)
        brain_mod.embed = fake_embed
        try:
            yield memory_dir, backup_dir
        finally:
            brain_mod.settings.memory_dir = original["memory_dir"]
            brain_mod.settings.memory_backup_dir = original["memory_backup_dir"]
            brain_mod.settings.memory_index_file = original["memory_index_file"]
            brain_mod.settings.memory_embeddings_file = original["memory_embeddings_file"]
            brain_mod.settings.memory_archive_file = original["memory_archive_file"]
            brain_mod.settings.memory_graph_file = original["memory_graph_file"]
            brain_mod.settings.memory_graph_relations_file = original["memory_graph_relations_file"]
            brain_mod.settings.memory_max_entries = original["memory_max_entries"]
            brain_mod.settings.memory_importance_min = original["memory_importance_min"]
            brain_mod.settings.memory_importance_max = original["memory_importance_max"]
            brain_mod.MEMORY_DIR = original["MEMORY_DIR"]
            brain_mod.BACKUP_DIR = original["BACKUP_DIR"]
            brain_mod.MEMORY_GRAPH_RELATIONS_PATH = original["MEMORY_GRAPH_RELATIONS_PATH"]
            brain_mod.embed = original["embed"]


class MemoryPureFunctionTests(unittest.TestCase):
    def test_quality_filter_rejects_known_vague_facts(self):
        cases = [
            ("short", True),
            ("User has medical records", True),
            ("doctor's offices nearby", True),
            ("User lives in an area with available", True),
            ("User listened to Despacito", True),
            ("User corrected the greeting from morning to afternoon", True),
            ("User mentioned the current time of day", True),
            ("Krish's confidence level in crush on Ankita is 0.32", True),
            ("User name is Krish Verma", False),
            ("User lives in Delhi", False),
            ("User is 15 years old", False),
            ("User has recovered from illness", False),
        ]

        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(_is_vague(text), expected)

    def test_contradiction_category_keeps_existing_contract(self):
        positive = [
            "User name is Krish",
            "User name is Krish Verma",
            "User lives in Delhi",
            "User lives in Maharashtra",
            "User is 15 years old",
            "User age is 15",
            "User is not feeling well",
            "User feels sick",
            "User has recovered from illness",
            "User now lives in Delhi",
        ]
        negative = ["Hello world", "The weather is nice"]

        for text in positive:
            with self.subTest(text=text):
                self.assertIsNotNone(_contradiction_category(text))
        for text in negative:
            with self.subTest(text=text):
                self.assertIsNone(_contradiction_category(text))

    def test_normalize_fact_without_regex(self):
        self.assertEqual(_normalize_fact("User now lives in Delhi"), "User lives in Delhi")
        self.assertEqual(_normalize_fact("User actually lives in Mumbai"), "User lives in Mumbai")
        self.assertEqual(_normalize_fact("User currently lives in Pune"), "User lives in Pune")
        self.assertEqual(_normalize_fact("User name is Krish"), "User name is Krish")
        self.assertNotIn("import re", Path("memory/brain.py").read_text(encoding="utf-8"))

    def test_memory_filter_policy_loads_reject_phrases_from_markdown(self):
        phrases = _load_policy_reject_phrases()

        self.assertIn("current time", phrases)
        self.assertIn("user corrected the greeting", phrases)

    def test_term_set_keeps_simple_singular_variant_without_regex(self):
        terms = _term_set("where does user lives", min_length=2)

        self.assertIn("lives", terms)
        self.assertIn("live", terms)

    def test_daily_memory_file_detection_excludes_index_artifacts(self):
        today = date.today().isoformat()

        self.assertEqual(_daily_date_from_memory_file(Path(f"memory_{today}.json")), today)
        self.assertIsNone(_daily_date_from_memory_file(Path("memory_index.json")))
        self.assertIsNone(_daily_date_from_memory_file(Path("memory_archive.jsonl")))
        self.assertIsNone(_daily_date_from_memory_file(Path("memory_not-a-date.json")))


class MemoryBrainTests(unittest.TestCase):
    def test_commit_contradictions_duplicates_and_vague_facts(self):
        with isolated_memory():
            brain = Brain()

            self.assertTrue(brain.commit("User lives in Maharashtra", importance=0.5))
            self.assertTrue(brain.commit("User lives in Delhi", importance=0.5))
            self.assertFalse(any(memory["text"] == "User lives in Maharashtra" for memory in brain.memories))
            self.assertTrue(any(memory["text"] == "User lives in Delhi" for memory in brain.memories))

            self.assertTrue(brain.commit("User is not feeling well", importance=0.5))
            self.assertTrue(brain.commit("User has recovered", importance=0.5))
            self.assertFalse(any("not feeling" in memory["text"] for memory in brain.memories))
            self.assertTrue(any("recovered" in memory["text"] for memory in brain.memories))

            before = len(brain.memories)
            self.assertFalse(brain.commit("User has medical records", importance=0.5))
            self.assertEqual(len(brain.memories), before)
            self.assertFalse(brain.commit("User lives in Delhi", importance=0.5))
            self.assertEqual(len(brain.memories), before)

    def test_empty_memory_persists_index_and_reports_full_coverage(self):
        with isolated_memory() as (memory_dir, _backup_dir):
            brain = Brain()
            assessment = brain.system_assessment()

            self.assertEqual(assessment["entry_count"], 0)
            self.assertEqual(assessment["index_coverage_ratio"], 1.0)
            self.assertTrue(assessment["index_present"])
            self.assertTrue((memory_dir / "memory_index.json").exists())
            self.assertTrue((memory_dir / "memory_embeddings.npy").exists())

    def test_loader_uses_only_daily_files_and_filters_runtime_noise(self):
        today = date.today().isoformat()
        payload = [
            {"text": "User lives in Delhi", "importance": 0.6, "ts": "10:00:00"},
            {"text": "User lives in Delhi", "importance": 0.6, "ts": "10:01:00"},
            {"text": "User has medical records", "importance": 0.9, "ts": "10:02:00"},
        ]

        with isolated_memory() as (memory_dir, _backup_dir):
            memory_dir.mkdir(parents=True, exist_ok=True)
            (memory_dir / f"memory_{today}.json").write_text(json.dumps(payload), encoding="utf-8")
            (memory_dir / "memory_index.json").write_text("[]", encoding="utf-8")
            (memory_dir / "memory_not-a-date.json").write_text("[]", encoding="utf-8")

            brain = Brain()
            daily_files = [path.name for path in brain._memory_files()]
            meta = json.loads((memory_dir / "memory_index.json").read_text(encoding="utf-8"))

            self.assertEqual(daily_files, [f"memory_{today}.json"])
            self.assertEqual([memory["text"] for memory in brain.memories], ["User lives in Delhi"])
            self.assertEqual(meta["source_files"], [f"memory_{today}.json"])

    def test_loader_filters_internal_confidence_artifacts(self):
        today = date.today().isoformat()
        payload = [
            {"text": "User name is Krish Verma", "importance": 0.8, "ts": "10:00:00"},
            {"text": "Krish's confidence level in crush on Ankita is 0.32", "importance": 0.8, "ts": "10:01:00"},
        ]

        with isolated_memory() as (memory_dir, _backup_dir):
            memory_dir.mkdir(parents=True, exist_ok=True)
            (memory_dir / f"memory_{today}.json").write_text(json.dumps(payload), encoding="utf-8")

            brain = Brain()

            self.assertEqual([memory["text"] for memory in brain.memories], ["User name is Krish Verma"])

    def test_importance_is_bounded_by_config(self):
        with isolated_memory(importance_min=0.2, importance_max=0.8):
            brain = Brain()

            self.assertTrue(brain.commit("User likes chess puzzles", importance=9))
            self.assertTrue(brain.commit("User works remotely from home", importance=-3))

            values = {memory["text"]: memory["importance"] for memory in brain.memories}
            self.assertEqual(values["User likes chess puzzles"], 0.8)
            self.assertEqual(values["User works remotely from home"], 0.2)

    def test_capacity_trim_archives_lowest_importance_entries(self):
        with isolated_memory(max_entries=2) as (memory_dir, _backup_dir):
            brain = Brain()

            self.assertTrue(brain.commit("User likes black coffee", importance=0.1))
            self.assertTrue(brain.commit("User lives in Delhi", importance=0.5))
            self.assertTrue(brain.commit("User works remotely from home", importance=0.9))

            remaining = [item["text"] for item in brain.memories]
            archive_text = (memory_dir / "memory_archive.jsonl").read_text(encoding="utf-8")

            self.assertEqual(len(remaining), 2)
            self.assertNotIn("User likes black coffee", remaining)
            self.assertIn('"reason": "capacity"', archive_text)

    def test_recall_rebuilds_stale_embedding_dimensions(self):
        vectors = {
            "User lives in Delhi": np.array([1.0, 0.0], dtype=np.float32),
            "where does the user live": np.array([1.0, 0.0], dtype=np.float32),
        }

        def fake_embed(texts, normalize=True):
            if isinstance(texts, str):
                return vectors[texts]
            return np.array([vectors[text] for text in texts], dtype=np.float32)

        with isolated_memory(fake_embed=fake_embed):
            brain = Brain()
            self.assertTrue(brain.commit("User lives in Delhi", importance=0.5))
            brain._embeddings = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)

            recalled = brain.recall("where does the user live")

            self.assertEqual(recalled, "User lives in Delhi")
            self.assertEqual(brain._embeddings.shape, (1, 2))

    def test_benchmark_recall_reports_average_latency(self):
        vectors = {
            "User lives in Delhi": np.array([1.0, 0.0], dtype=np.float32),
            "where does the user live": np.array([1.0, 0.0], dtype=np.float32),
        }

        def fake_embed(texts, normalize=True):
            if isinstance(texts, str):
                return vectors[texts]
            return np.array([vectors[text] for text in texts], dtype=np.float32)

        with isolated_memory(fake_embed=fake_embed):
            brain = Brain()
            self.assertTrue(brain.commit("User lives in Delhi", importance=0.5))

            report = brain.benchmark_recall("where does the user live", runs=3, k=1)

            self.assertEqual(report["runs"], 3)
        self.assertEqual(report["result_count"], 1)
        self.assertGreaterEqual(report["avg_ms"], 0.0)

    def test_remember_and_forget_exact_memory(self):
        with isolated_memory():
            brain = Brain()

            remembered = brain.remember("User prefers grounded assistant answers")
            forgotten = brain.forget("grounded assistant answers")

            self.assertTrue(remembered["stored"])
            self.assertEqual(forgotten["status"], "removed")
            self.assertEqual(forgotten["removed"], ["User prefers grounded assistant answers"])
            self.assertFalse(brain.memories)

    def test_graph_rules_create_active_triples(self):
        with isolated_memory() as (memory_dir, _backup_dir):
            brain = Brain()

            self.assertTrue(brain.commit("User likes Python", importance=0.9))
            summary = brain.graph_summary("coding Python")
            graph_file = memory_dir / "memory_graph.json"
            graph = json.loads(graph_file.read_text(encoding="utf-8"))

            self.assertIn("User likes Python", summary)
            self.assertEqual(graph["nodes"]["user"]["type"], "person")
            self.assertTrue(any(edge["relation"] == "likes" and edge["active"] for edge in graph["edges"]))

    def test_recall_context_includes_graph_memory_without_changing_recall_contract(self):
        vectors = {
            "User likes Python": np.array([1.0, 0.0], dtype=np.float32),
            "what should I use for coding": np.array([0.0, 1.0], dtype=np.float32),
            "what Python preference is stored": np.array([0.0, 1.0], dtype=np.float32),
        }

        def fake_embed(texts, normalize=True):
            if isinstance(texts, str):
                return vectors[texts]
            return np.array([vectors[text] for text in texts], dtype=np.float32)

        with isolated_memory(fake_embed=fake_embed):
            brain = Brain()
            self.assertTrue(brain.commit("User likes Python", importance=0.9))

            self.assertEqual(brain.recall("what should I use for coding"), "")
            self.assertIn("Graph memory: User likes Python", brain.recall_context("what Python preference is stored"))

    def test_temporal_graph_edges_supersede_old_provider(self):
        with isolated_memory():
            brain = Brain()

            self.assertTrue(brain.commit("Ankita uses Cohere", importance=0.7))
            self.assertTrue(brain.commit("Ankita now uses Groq", importance=0.9))

            graph = brain._graph
            active = [edge for edge in graph["edges"] if edge["relation"] == "uses" and edge["active"]]
            inactive = [edge for edge in graph["edges"] if edge["relation"] == "uses" and not edge["active"]]
            summary = brain.graph_summary("Ankita provider")
            context = brain.recall_context("Ankita provider")

            self.assertEqual(len(active), 1)
            self.assertEqual(len(inactive), 1)
            self.assertIn("Ankita uses Groq", summary)
            self.assertNotIn("Ankita uses Cohere", summary)
            self.assertIn("Ankita now uses Groq", context)
            self.assertNotIn("Ankita uses Cohere", context)

    def test_temporal_contradictions_are_scoped_to_subject_and_relation(self):
        with isolated_memory():
            brain = Brain()

            self.assertTrue(brain.commit("User lives in Delhi", importance=0.8))
            self.assertTrue(brain.commit("Ankita lives in Haryana", importance=0.8))
            self.assertTrue(brain.commit("Ankita lives in Mumbai", importance=0.9))

            memories = [memory["text"] for memory in brain.memories]

            self.assertIn("User lives in Delhi", memories)
            self.assertNotIn("Ankita lives in Haryana", memories)
            self.assertIn("Ankita lives in Mumbai", memories)

    def test_ranked_recall_returns_scores_and_confidence(self):
        vectors = {
            "User likes Python": np.array([1.0, 0.0], dtype=np.float32),
            "User likes Java": np.array([0.6, 0.4], dtype=np.float32),
            "Python preference": np.array([1.0, 0.0], dtype=np.float32),
        }

        def fake_embed(texts, normalize=True):
            if isinstance(texts, str):
                return vectors[texts]
            return np.array([vectors[text] for text in texts], dtype=np.float32)

        with isolated_memory(fake_embed=fake_embed):
            brain = Brain()
            self.assertTrue(brain.commit("User likes Python", importance=0.9))
            self.assertTrue(brain.commit("User likes Java", importance=0.4))

            ranked = brain.recall_ranked("Python preference", k=2)

            self.assertEqual(ranked[0]["text"], "User likes Python")
            self.assertIn("score", ranked[0])
            self.assertIn("confidence", ranked[0])
            self.assertGreaterEqual(ranked[0]["confidence"], 0.0)
            self.assertLessEqual(ranked[0]["confidence"], 1.0)

    def test_graph_infers_crush_academic_status(self):
        with isolated_memory():
            brain = Brain()

            self.assertTrue(brain.commit("My crush is Ankita", importance=0.9))
            self.assertTrue(brain.commit("Ankita is in class 11th", importance=0.9))

            summary = brain.graph_summary("what class is my crush in")
            context = brain.recall_context("what class is my crush in")

            self.assertIn("User crush Ankita -> Ankita in class 11th", summary)
            self.assertIn("Graph memory:", context)
            self.assertIn("Ankita in class 11th", context)
            self.assertNotIn("confidence", summary.casefold())
            self.assertNotIn("confidence", context.casefold())

    def test_profile_context_lists_broad_facts_without_confidence_metadata(self):
        with isolated_memory():
            brain = Brain()

            self.assertTrue(brain.commit("User name is Krish Verma", importance=0.9))
            self.assertTrue(brain.commit("User lives in Delhi", importance=0.9))
            self.assertTrue(brain.commit("My crush is Ankita", importance=0.9))
            self.assertTrue(brain.commit("Ankita likes short hair", importance=0.8))

            context = brain.profile_context(limit=8)

            self.assertIn("User name is Krish Verma", context)
            self.assertIn("User lives in Delhi", context)
            self.assertIn("User crush Ankita", context)
            self.assertIn("Ankita likes short hair", context)
            self.assertNotIn("confidence", context.casefold())

    def test_frontend_raw_personal_facts_create_graph_relations(self):
        with isolated_memory():
            brain = Brain()

            self.assertTrue(brain.commit("my cursh is ankita", importance=0.8))
            self.assertTrue(brain.commit("ankita lives in home", importance=0.8))

            summary = brain.graph_summary("ankita home crush")
            active_edges = [edge for edge in brain._graph["edges"] if edge["active"]]

            self.assertIn("User crush ankita", summary)
            self.assertIn("ankita lives in home", summary)
            self.assertTrue(any(edge["relation"] == "crush" for edge in active_edges))
            self.assertTrue(any(edge["relation"] == "lives_in" for edge in active_edges))

    def test_graph_summary_does_not_return_unrelated_edges_for_chat(self):
        with isolated_memory():
            brain = Brain()

            self.assertTrue(brain.commit("my cursh is ankita", importance=0.8))

            self.assertEqual(brain.graph_summary("how are you"), "")
            self.assertIn("User crush ankita", brain.graph_summary("who is my crush huh"))

    def test_graph_summary_matches_live_to_lives_relation(self):
        with isolated_memory():
            brain = Brain()

            self.assertTrue(brain.commit("User lives in Delhi", importance=0.9))

            self.assertIn("User lives in Delhi", brain.graph_summary("where do I live"))

    def test_reflection_records_graph_insight(self):
        with isolated_memory():
            brain = Brain()
            self.assertTrue(brain.commit("User is building Ankita", importance=0.9))
            self.assertTrue(brain.commit("Ankita uses Python", importance=0.8))

            reflection = brain.reflect("unit-test")

            self.assertEqual(reflection["label"], "unit-test")
            self.assertIn("active relations", reflection["summary"])
            self.assertEqual(brain.graph_assessment()["reflection_count"], 1)


if __name__ == "__main__":
    unittest.main()
