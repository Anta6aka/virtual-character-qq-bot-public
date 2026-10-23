"""Relationship service integration against a temporary database."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "AstrBot/data/plugins/astrbot_plugin_ltm_fts"))

from relationship.service import RelationshipService, ServiceConfig  # noqa: E402

NOW = 1792800000.0


class RelationshipServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "memory.db"
        self.service = RelationshipService(
            self.path, "char_a",
            config=ServiceConfig(enabled=True, character_id="char_a", max_visible=3),
            clock=lambda: NOW)

    def tearDown(self):
        self.service.close()

    def test_capture_does_not_raise(self):
        result = self.service.on_message(
            platform="test", session="s1", group_id="g1", member_id="m1",
            message_id="m1", text="今天聊得很开心", at=NOW, directed=True)
        self.assertTrue(result.enabled)
        self.assertEqual(result.error, "")

    def test_prompt_parts_after_capture(self):
        self.service.on_message(
            platform="test", session="s1", group_id="g1", member_id="m1",
            message_id="m1", text="今天聊得很开心", at=NOW, directed=True)
        parts = self.service.prompt_parts(
            platform="test", session="s1", group_id="g1", member_id="m1",
            query="今天", at=NOW)
        self.assertIsInstance(parts, dict)

    def test_reply_commit_is_a_dict(self):
        self.service.on_message(
            platform="test", session="s1", group_id="g1", member_id="m1",
            message_id="m1", text="今天聊得很开心", at=NOW, directed=True)
        result = self.service.on_reply_sent(
            platform="test", session="s1", group_id="g1", member_id="m1",
            message_id="r1", incoming_text="今天聊得很开心",
            reply_text="我也很开心", at=NOW, confirmed=True)
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
