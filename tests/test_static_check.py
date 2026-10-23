"""QP-27: repository static checks run standalone and detect regressions."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import static_check

ROOT = Path(__file__).resolve().parents[1]


class StaticCheckTests(unittest.TestCase):
    def test_repository_is_clean(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(ROOT / "static_check.py")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("static checks OK", result.stdout)

    def test_secret_pattern_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "leak.py"
            path.write_text("TOKEN = 'sk-" + "a" * 24 + "'", encoding="utf-8")
            problems = []
            static_check.check_forbidden([path], problems)
            self.assertTrue(any("secret:" in item for item in problems), problems)

    def test_private_config_name_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "peer_roles.json"
            path.write_text("{}", encoding="utf-8")
            problems = []
            static_check.check_forbidden([path], problems)
            self.assertTrue(any("private config tracked" in item for item in problems), problems)

    def test_missing_markdown_link_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            doc = Path(folder) / "doc.md"
            doc.write_text("[x](missing.md)", encoding="utf-8")
            problems = []
            static_check.check_markdown_links([doc], problems, set())
            self.assertEqual(len(problems), 1, problems)

    def test_invalid_python_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.py"
            path.write_text("def broken(:\n", encoding="utf-8")
            problems = []
            static_check.check_python([path], problems)
            self.assertEqual(len(problems), 1, problems)


if __name__ == "__main__":
    unittest.main()
