"""Pure relationship engine tests: evidence, duplicates and affinity."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "AstrBot/data/plugins/astrbot_plugin_ltm_fts"))

from relationship import engine  # noqa: E402

NOW = 1792800000.0
DAY = engine.day_for(NOW)


def evidence(event_id="e1", kind="trust_positive", strength="normal"):
    return engine.Evidence(
        event_id=event_id, kind=kind, strength=strength, subject="member",
        source_session="s1", source_message_id="m1", fingerprint="fp-" + event_id,
        at=NOW, verified=True, meaningful=False, day=DAY)


class RelationshipEngineTests(unittest.TestCase):
    def test_trust_evidence_changes_state_without_mutating_input(self):
        state = engine.RelationshipState()
        counters = engine.DailyCounters(day=DAY)
        result = engine.evaluate(
            state, counters, [evidence()],
            seen_event_ids=set(), seen_fingerprints={}, seats_taken=0, now=NOW)
        self.assertTrue(result.decisions[0].accepted)
        self.assertGreater(result.state.trust, state.trust)
        self.assertEqual(state.trust, 50.0)
        self.assertEqual(counters.t_positive, 0.0)

    def test_duplicate_event_is_rejected(self):
        state = engine.RelationshipState()
        counters = engine.DailyCounters(day=DAY)
        result = engine.evaluate(
            state, counters, [evidence()],
            seen_event_ids={"e1"}, seen_fingerprints={}, seats_taken=0, now=NOW)
        self.assertFalse(result.decisions[0].accepted)
        self.assertEqual(result.state.trust, state.trust)

    def test_empty_batch_is_a_noop(self):
        state = engine.RelationshipState()
        counters = engine.DailyCounters(day=DAY)
        result = engine.evaluate(
            state, counters, [], seen_event_ids=set(), seen_fingerprints={},
            seats_taken=0, now=NOW)
        self.assertEqual(result.decisions, ())
        self.assertEqual(result.state.trust, state.trust)

    def test_affinity_mapping(self):
        self.assertEqual(engine.relationship_affinity(engine.RelationshipState(stage="initial")), 0)
        self.assertEqual(engine.relationship_affinity(engine.RelationshipState(stage="familiar")), 1)
        self.assertEqual(engine.relationship_affinity(engine.RelationshipState(stage="trusted")), 1)
        self.assertEqual(engine.relationship_affinity(engine.RelationshipState(stage="unknown")), 0)


if __name__ == "__main__":
    unittest.main()
