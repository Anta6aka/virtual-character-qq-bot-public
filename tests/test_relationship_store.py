"""Relationship store tests against a temporary SQLite database."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "AstrBot/data/plugins/astrbot_plugin_ltm_fts"))

from relationship.identity import MemberIdentity, Scene  # noqa: E402
from relationship.store import RelationshipStore  # noqa: E402

NOW = 1792800000.0


class RelationshipStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "memory.db"
        self.store = RelationshipStore(self.path, clock=lambda: NOW)
        self.info = self.store.migrate(character_id="char_a")
        self.identity = MemberIdentity("char_a", "test", "m1")
        self.scene = Scene("group", "s1", "g1", "test")

    def test_migration_reports_schema(self):
        self.assertIsInstance(self.info, dict)
        self.assertGreaterEqual(int(self.info.get("schema_version", 0)), 1)
        self.assertTrue(self.path.exists())

    def test_pending_evidence_is_committed(self):
        self.store.add_pending(self.identity, self.scene, message_id="m1",
                               text="今天聊得很开心，谢谢你", at=NOW)
        result = self.store.evaluate_commit(self.identity, self.scene,
                                            message_id="m1", at=NOW)
        self.assertIsInstance(result, dict)
        connection = sqlite3.connect(self.path)
        try:
            count = connection.execute("select count(*) from rel_members").fetchone()[0]
        finally:
            connection.close()
        self.assertGreaterEqual(count, 1)

    def test_visible_items_returns_a_list(self):
        self.store.record_source(self.identity, self.scene, message_id="s1",
                                 text="我喜欢推理小说和咖啡", at=NOW)
        items = self.store.visible_items(
            self.identity, Scene("group", "s2", "g2", "test"),
            query="推理小说", now=NOW)
        self.assertIsInstance(items, list)

    def test_reopening_keeps_the_database(self):
        self.store.migrate(character_id="char_a")
        reopened = RelationshipStore(self.path, clock=lambda: NOW)
        info = reopened.schema_info()
        self.assertIsInstance(info, dict)
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
