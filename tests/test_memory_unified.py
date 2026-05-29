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

            # The memory worker writes directly to the vault root
            vault_root = Path(brain_mod.settings.memory_obsidian_vault_dir)

            self.assertIn(stored["obsidian_graph"]["status"], {"current", "synced"})
            self.assertTrue(vault_root.exists())
            self.assertTrue((vault_root / "Index.md").exists())

    @patch.object(brain_mod, "embed", _ones_embed)
    def test_relationship_facts_create_person_pages(self):
        with isolated_memory():
            brain = brain_mod.Brain()
            self.assertTrue(brain.commit("Ankita is my girlfriend"))
            self.assertTrue(brain.commit("User has a friend named Rai Bud"))
            self.assertTrue(
                brain.commit("Ankita does not know that Rai is my friend, but Rai knows about Ankita")
            )

            nodes = brain._graph.get("nodes", {})
            self.assertEqual(nodes.get("ankita", {}).get("type"), "person")
            self.assertEqual(nodes.get("rai_bud", {}).get("type"), "person")
            self.assertEqual(nodes.get("rai", {}).get("type"), "person")

            vault_root = Path(brain_mod.settings.memory_obsidian_vault_dir)
            ankita_page = (vault_root / "People" / "Ankita.md").read_text(encoding="utf-8")
            rai_bud_page = (vault_root / "People" / "Rai Bud.md").read_text(encoding="utf-8")
            self.assertTrue(ankita_page.startswith("---"))
            self.assertTrue(rai_bud_page.startswith("---"))
            self.assertNotIn("```", ankita_page)
            self.assertNotIn("```", rai_bud_page)
            self.assertIn("- User has a friend named Rai Bud", rai_bud_page)
            timeline = (vault_root / "Timeline" / f"{brain.memories[-1].get('_date')}.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("[[People/Ankita|Ankita]]", timeline)
            self.assertIn("[[People/Rai Bud|Rai Bud]]", timeline)

    @patch.object(brain_mod, "embed", _ones_embed)
    def test_graph_rebuild_replaces_relationship_fallback_edges(self):
        with isolated_memory():
            brain = brain_mod.Brain()
            self.assertTrue(brain.commit("Ankita is my girlfriend"))
            memory = brain.memories[-1]
            old_target = brain._ensure_graph_node(memory["text"], "memory", memory.get("importance", 0.5))
            old_edge = brain._edge_id("user", "remembers", old_target)
            brain._graph["edges"] = [
                {
                    "id": old_edge,
                    "source": "user",
                    "target": old_target,
                    "relation": "remembers",
                    "memory_id": memory["id"],
                    "active": True,
                }
            ]
            brain._graph["memory_links"] = {memory["id"]: [old_edge]}
            memory["graph_edges"] = [old_edge]
            memory["graph_nodes"] = ["user", old_target]

            report = brain.maintain(rebuild=True, backup=False)

            self.assertTrue(report.get("graph_rebuilt"))
            self.assertEqual(brain._graph.get("nodes", {}).get("ankita", {}).get("type"), "person")
            self.assertNotIn(old_target, memory.get("graph_nodes", []))


if __name__ == "__main__":
    unittest.main()
