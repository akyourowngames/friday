import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory.brain as brain_mod
from tests.test_memory import isolated_memory, _ones_embed


class UnifiedMemoryTests(unittest.TestCase):
    @patch.object(brain_mod, "embed", _ones_embed)
    def test_commit_stores_unified_graph_refs(self):
        with isolated_memory() as (memory_dir, _backup):
            brain = brain_mod.Brain()
            self.assertTrue(brain.commit("User name is Krish Verma"))
            memory = brain.memories[-1]
            self.assertEqual(memory.get("storage"), "graph")
            self.assertTrue(memory.get("graph_edges"))
            self.assertTrue(memory.get("graph_nodes"))
            graph = json.loads((memory_dir / brain_mod.settings.memory_graph_file).read_text(encoding="utf-8"))
            self.assertIn("memory_links", graph)
            self.assertIn(memory["id"], graph.get("memory_links", {}))

    @patch.object(brain_mod, "embed", _ones_embed)
    def test_auto_relations_for_comentioned_entities(self):
        with isolated_memory():
            brain = brain_mod.Brain()
            brain.commit("User name is Krish Verma")
            brain.commit("Ankita is in class 11th")
            brain.commit("Krish and Ankita study together")
            relations = {
                edge.get("relation")
                for edge in brain._graph.get("edges", [])
                if edge.get("active", True)
            }
            self.assertIn("associated_with", relations)

    @patch.object(brain_mod, "embed", _ones_embed)
    def test_recall_context_is_unified_not_split(self):
        with isolated_memory():
            brain = brain_mod.Brain()
            brain.commit("User lives in Bangalore")
            context = brain.recall_context("where do I live", k=3)
            self.assertIn("Bangalore", context)
            self.assertNotIn("Text memory:", context)
            self.assertNotIn("Graph memory:", context)

    @patch.object(brain_mod, "embed", _ones_embed)
    def test_recall_unified_returns_sources(self):
        with isolated_memory():
            brain = brain_mod.Brain()
            brain.commit("User prefers dark mode")
            hits = brain.recall_unified("dark mode preference", k=3)
            self.assertTrue(hits)
            self.assertTrue(hits[0].get("sources"))

    @patch.object(brain_mod, "embed", _ones_embed)
    def test_obsidian_graph_sync_tracks_remember_and_forget(self):
        with isolated_memory():
            brain = brain_mod.Brain()
            stored = brain.remember("User prefers dark mode")
            graph_root = (
                Path(brain_mod.settings.memory_obsidian_vault_dir)
                / brain_mod.settings.memory_obsidian_graph_dir
            )

            self.assertIn(stored["obsidian_graph"]["status"], {"current", "synced"})
            self.assertTrue((graph_root / "Index.md").exists())
            self.assertTrue((graph_root / "Nodes" / "user.md").exists())
            self.assertTrue((graph_root / "Nodes" / "dark_mode.md").exists())
            self.assertTrue(list((graph_root / "Memories").glob("*.md")))
            user_page = (graph_root / "Nodes" / "user.md").read_text(encoding="utf-8")
            self.assertIn("[[Generated Memory Graph/Nodes/dark_mode|dark mode]]", user_page)

            removed = brain.forget("dark mode")

            self.assertIn(removed["obsidian_graph"]["status"], {"current", "synced"})
            self.assertFalse((graph_root / "Nodes" / "dark_mode.md").exists())
            removed_page = (graph_root / "Removed Memory.md").read_text(encoding="utf-8")
            self.assertIn("dark mode", removed_page)


if __name__ == "__main__":
    unittest.main()
