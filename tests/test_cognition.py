"""Tests for the KING cognition substrate.

Offline and deterministic: no network, no live embedder. Embedding-dependent
paths are exercised with a tiny fake embed function.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cognition.cadence import CadenceModel
from cognition.config import load_cognition_config, section_values
from cognition.episodes import stitch_episodes
from cognition.proactive import Candidate, ProactiveEngine
from cognition.situation import SituationModel
from cognition import orchestrator, state as cognition_state


def _fake_embed(texts, normalize=True):
    """Deterministic toy embedder: maps text length parity + first char to a
    small vector so similar-looking strings cluster and others don't."""
    def one(text: str):
        seed = sum(ord(c) for c in text) % 7
        vec = np.zeros(4, dtype=np.float32)
        vec[seed % 4] = 1.0
        return vec

    if isinstance(texts, str):
        return one(texts)
    return np.array([one(t) for t in texts], dtype=np.float32)


class ConfigTests(unittest.TestCase):
    def test_config_file_loads_sections(self):
        config = load_cognition_config()
        self.assertIn("proactive", config)
        self.assertIn("situation", config)
        self.assertIsInstance(config["proactive"].get("daily_budget"), int)

    def test_section_values_merges_over_defaults(self):
        defaults = {"daily_budget": 99, "unknown_key": 1}
        merged = section_values("proactive", defaults)
        # File overrides the known default; unknown_key stays untouched.
        self.assertEqual(merged["unknown_key"], 1)
        self.assertNotEqual(merged["daily_budget"], 99)


class SituationTests(unittest.TestCase):
    def _model(self):
        return SituationModel(
            config={
                "busy_event_window_seconds": 120,
                "busy_event_count_for_full_load": 4,
                "idle_seconds_for_available": 600,
                "rapid_turn_window_seconds": 90,
                "rapid_turn_count_for_engaged": 3,
                "load_decay_half_life_seconds": 300,
                "min_availability_to_speak": 0.45,
                "max_load_to_speak": 0.6,
            }
        )

    def test_no_signal_is_available(self):
        model = self._model()
        self.assertTrue(model.can_interrupt())
        self.assertEqual(model.availability(), 1.0)

    def test_burst_of_events_raises_load_and_blocks(self):
        model = self._model()
        now = datetime(2026, 5, 29, 10, 0, 0)
        for offset in range(6):
            model.record_event(now - timedelta(seconds=offset))
        self.assertGreater(model.cognitive_load(now), 0.6)
        self.assertFalse(model.can_interrupt(now))

    def test_load_decays_after_quiet(self):
        model = self._model()
        now = datetime(2026, 5, 29, 10, 0, 0)
        for offset in range(6):
            model.record_event(now - timedelta(seconds=offset))
        later = now + timedelta(seconds=1800)
        self.assertLess(model.cognitive_load(later), 0.1)
        self.assertTrue(model.can_interrupt(later))


class CadenceTests(unittest.TestCase):
    def test_missing_expected_deviation(self):
        model = CadenceModel(
            config={
                "buckets_per_day": 24,
                "ema_alpha": 0.3,
                "min_observations_for_signal": 3,
                "deviation_min_strength": 0.3,
                "expected_activity_floor": 0.1,
                "max_tracked_nodes": 200,
            }
        )
        # Observe activity every weekday around 09:00 for several weeks.
        base = datetime(2026, 4, 6, 9, 0, 0)  # a Monday
        for week in range(4):
            for weekday in range(5):
                when = base + timedelta(weeks=week, days=weekday)
                model.observe("king_repo", when=when)
        # Now it is a later weekday 09:05 but the node has not been seen today.
        now = datetime(2026, 5, 11, 9, 5, 0)  # Monday, ~ peak bucket, long gap
        report = model.deviation("king_repo", now=now)
        self.assertEqual(report["kind"], "missing_expected")
        self.assertTrue(report["actionable"])
        self.assertGreater(report["strength"], 0.3)

    def test_serialization_round_trip(self):
        model = CadenceModel()
        model.observe("alpha", when=datetime(2026, 5, 1, 8, 0, 0))
        model.observe("alpha", when=datetime(2026, 5, 2, 8, 0, 0))
        restored = CadenceModel.from_dict(model.to_dict())
        self.assertIn("alpha", restored.nodes)
        self.assertEqual(restored.nodes["alpha"]["observations"], 2)


class EpisodeTests(unittest.TestCase):
    def _memories(self):
        return [
            {"id": "m1", "text": "Started the gesture module", "_date": "2026-05-01", "ts": "10:00:00"},
            {"id": "m2", "text": "Started the gesture trainer", "_date": "2026-05-01", "ts": "10:30:00"},
            {"id": "m3", "text": "Took a long break from coding", "_date": "2026-05-20", "ts": "15:00:00"},
            {"id": "m4", "text": "Took a long break again later", "_date": "2026-05-20", "ts": "15:20:00"},
        ]

    def test_time_proximity_clusters(self):
        episodes = stitch_episodes(
            self._memories(),
            embed_fn=None,
            config={
                "time_gap_minutes": 180,
                "similarity_link_threshold": 0.55,
                "min_episode_size": 2,
                "max_episode_size": 40,
                "max_episodes": 60,
                "title_max_chars": 80,
            },
        )
        # Two distinct time clusters -> two episodes.
        self.assertEqual(len(episodes), 2)
        for episode in episodes:
            self.assertEqual(episode["size"], 2)
            self.assertTrue(episode["title"])

    def test_titler_callback_used(self):
        episodes = stitch_episodes(
            self._memories(),
            embed_fn=None,
            titler=lambda texts: "custom title",
        )
        self.assertTrue(episodes)
        self.assertEqual(episodes[0]["title"], "custom title")


class ProactiveTests(unittest.TestCase):
    def _engine(self):
        return ProactiveEngine(config=self._config())

    def _config(self):
        return {
            "base_confidence_threshold": 0.4,
            "threshold_rise_after_speak": 0.4,
            "threshold_decay_half_life_seconds": 3600,
            "daily_budget": 2,
            "novelty_suppression_similarity": 0.8,
            "relevance_weight": 0.4,
            "freshness_weight": 0.2,
            "importance_weight": 0.2,
            "situational_weight": 0.2,
            "freshness_half_life_seconds": 86400,
            "annoyance_penalty_per_dismissal": 0.2,
            "max_queue_size": 50,
        }

    def test_high_value_candidate_selected(self):
        engine = self._engine()
        now = datetime(2026, 5, 29, 10, 0, 0)
        engine.add_candidate(Candidate("notice X", "cadence_missing_expected", importance=0.9, relevance=0.9, created_at=now.isoformat()))
        chosen = engine.select(situational_fit=0.9, now=now)
        self.assertIsNotNone(chosen)

    def test_low_value_candidate_rejected(self):
        engine = self._engine()
        now = datetime(2026, 5, 29, 10, 0, 0)
        engine.add_candidate(Candidate("weak", "cadence", importance=0.05, relevance=0.05, created_at=now.isoformat()))
        self.assertIsNone(engine.select(situational_fit=0.1, now=now))

    def test_threshold_rises_after_speaking(self):
        engine = self._engine()
        now = datetime(2026, 5, 29, 10, 0, 0)
        before = engine.current_threshold(now)
        candidate = Candidate("notice", "src", importance=0.9, relevance=0.9, created_at=now.isoformat())
        engine.add_candidate(candidate)
        engine.mark_delivered(candidate, now=now)
        after = engine.current_threshold(now)
        self.assertGreater(after, before)

    def test_daily_budget_enforced(self):
        engine = self._engine()
        now = datetime(2026, 5, 29, 10, 0, 0)
        for index in range(3):
            engine.mark_delivered(
                Candidate(f"c{index}", "src", created_at=now.isoformat()),
                now=now + timedelta(minutes=index),
            )
        self.assertEqual(engine.budget_remaining(now), 0)
        engine.add_candidate(Candidate("new", "src", importance=0.9, relevance=0.9, created_at=now.isoformat()))
        self.assertIsNone(engine.select(situational_fit=0.9, now=now))

    def test_novelty_suppression(self):
        engine = self._engine()
        now = datetime(2026, 5, 29, 10, 0, 0)
        emb = [1.0, 0.0, 0.0, 0.0]
        delivered = Candidate("first", "src", importance=0.9, relevance=0.9, created_at=now.isoformat(), embedding=emb)
        engine.mark_delivered(delivered, now=now)
        engine.add_candidate(Candidate("near duplicate", "src", importance=0.9, relevance=0.9, created_at=now.isoformat(), embedding=emb))
        self.assertIsNone(engine.select(situational_fit=0.9, now=now))

    def test_dismissal_penalizes_source(self):
        engine = self._engine()
        now = datetime(2026, 5, 29, 10, 0, 0)
        candidate = Candidate("c", "noisy_source", importance=0.6, relevance=0.6, created_at=now.isoformat())
        clean = engine.score(candidate, situational_fit=0.6, now=now)
        engine.record_dismissal("noisy_source")
        engine.record_dismissal("noisy_source")
        penalized = engine.score(candidate, situational_fit=0.6, now=now)
        self.assertLess(penalized, clean)

    def test_state_round_trip(self):
        engine = self._engine()
        now = datetime(2026, 5, 29, 10, 0, 0)
        engine.mark_delivered(Candidate("c", "src", created_at=now.isoformat()), now=now)
        engine.record_dismissal("src")
        restored = ProactiveEngine.from_dict(engine.to_dict(), config=self._config())
        self.assertEqual(restored.budget_remaining(now), engine.budget_remaining(now))


class _FakeBrain:
    def __init__(self, memories):
        self.memories = memories


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "cognition_state.json"
        self._orig = cognition_state.settings.cognition_state_path
        cognition_state.settings.cognition_state_path = str(self.state_path)
        self.addCleanup(self._restore)

    def _restore(self):
        cognition_state.settings.cognition_state_path = self._orig

    def _memories(self):
        memories = []
        base = datetime(2026, 4, 6, 9, 0, 0)
        for week in range(4):
            for weekday in range(5):
                when = base + timedelta(weeks=week, days=weekday)
                memories.append(
                    {
                        "id": f"m_{week}_{weekday}",
                        "text": f"Worked on king repo session {week}-{weekday}",
                        "_date": when.date().isoformat(),
                        "ts": when.strftime("%H:%M:%S"),
                        "graph_nodes": ["king_repo"],
                    }
                )
        return memories

    def test_pass_persists_state_and_builds_cadence(self):
        brain = _FakeBrain(self._memories())
        now = datetime(2026, 5, 11, 9, 5, 0)
        result = orchestrator.run_cognition_pass(brain, embed_fn=_fake_embed, now=now)
        self.assertGreater(result["memories_seen"], 0)
        self.assertIn("king_repo", CadenceModel.from_dict(cognition_state.load_state()["cadence"]).nodes)
        self.assertTrue(self.state_path.exists())
        self.assertGreaterEqual(result["episodes"], 1)

    def test_pass_with_empty_brain_is_safe(self):
        brain = _FakeBrain([])
        result = orchestrator.run_cognition_pass(brain, embed_fn=_fake_embed)
        self.assertEqual(result["memories_seen"], 0)
        self.assertEqual(result["episodes"], 0)


class MaintenanceStepTests(unittest.TestCase):
    def test_cognition_scan_registered(self):
        from maintenance.engine import MaintenanceEngine
        from maintenance.config import MaintenanceConfig
        from maintenance.steps import register_default_steps

        config = MaintenanceConfig(repo_root=Path("."), config_path=Path("MAINTENANCE_DAILY.md"))
        engine = MaintenanceEngine(config)
        register_default_steps(engine)
        self.assertIn("cognition_scan", engine.status()["registered_handlers"])


if __name__ == "__main__":
    unittest.main()
