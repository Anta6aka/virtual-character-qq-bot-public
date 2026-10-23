"""QP-22: temp cleanup planning only deletes stale files and never touches backups."""
import os
from pathlib import Path
import tempfile
import time
import unittest

import cleanup_temp as cleanup


class CleanupTempTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "temp"
        self.root.mkdir()

    def make(self, name, age_days, size=10):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_only_stale_files_are_listed(self):
        old = self.make("old.log", 30)
        self.make("new.log", 1)
        self.make("nested/old.log", 40)
        items = cleanup.stale_files(self.root, 14)
        names = {path.name for path, _ in items}
        self.assertIn(old.name, names)
        self.assertNotIn("new.log", names)
        self.assertEqual(len(items), 2)
        self.assertGreater(cleanup.summarize(items)[1], 0)

    def test_remove_deletes_only_listed_files(self):
        old = self.make("old.log", 30)
        fresh = self.make("fresh.log", 1)
        items = cleanup.stale_files(self.root, 14)
        self.assertEqual(cleanup.remove(items), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_backups_tree_is_refused(self):
        with self.assertRaises(ValueError):
            cleanup.stale_files(cleanup.ROOT / "backups", 1)
        with self.assertRaises(ValueError):
            cleanup.guard_root(cleanup.ROOT / "backups" / "20260101_snapshot")

    def test_missing_root_returns_empty(self):
        self.assertEqual(cleanup.stale_files(Path(self.temporary.name) / "absent", 14), [])

    def test_guard_allows_an_explicit_temp_root(self):
        self.assertEqual(cleanup.guard_root(self.root), self.root.resolve())

    def test_repository_roots_are_refused(self):
        for target in (cleanup.ROOT, cleanup.ROOT / "AstrBot", cleanup.ROOT / "tools",
                       cleanup.ROOT / "templates", cleanup.ROOT.parent):
            with self.subTest(target=str(target)):
                with self.assertRaises(ValueError):
                    cleanup.guard_root(target)
        with self.assertRaises(ValueError):
            cleanup.stale_files(cleanup.ROOT, 1)

    def test_project_temp_root_is_allowed(self):
        self.assertEqual(cleanup.guard_root(cleanup.TEMP_ROOT), cleanup.TEMP_ROOT.resolve())
        self.assertEqual(cleanup.guard_root(cleanup.TEMP_ROOT / "tests"), (cleanup.TEMP_ROOT / "tests").resolve())


if __name__ == "__main__":
    unittest.main()
