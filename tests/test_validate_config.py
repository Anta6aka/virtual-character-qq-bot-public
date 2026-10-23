"""Config validator tests for public examples and common mistakes."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import validate_config  # noqa: E402


class ValidateConfigTests(unittest.TestCase):
    def test_public_examples_are_valid(self):
        for relative, kind in (
            ("examples/connection_guard.example.json", "guard"),
            ("examples/relationship.example.json", "relationship"),
            ("examples/relationship_params.example.json", "params"),
            ("examples/character_profile.example.json", "profile"),
            ("examples/sticker_groups.example.json", "groups"),
            ("examples/demo/relationship.json", "relationship"),
            ("examples/demo/character_profile.json", "profile"),
            ("examples/demo/sticker_groups.demo.json", "groups"),
        ):
            with self.subTest(relative=relative):
                self.assertEqual(validate_config.validate_file(ROOT / relative, kind), [])

    def test_relationship_ranges(self):
        problems = validate_config.validate_relationship(
            {"enable": True, "character_id": "char_a", "max_visible": 11})
        self.assertTrue(any("max_visible" in item for item in problems))

    def test_duplicate_groups(self):
        problems = validate_config.validate_groups(
            {"schema": 1, "groups": ["10001", "10001"]})
        self.assertTrue(any("unique" in item for item in problems))

    def test_invalid_regex(self):
        problems = validate_config.validate_profile({
            "schema": 1, "name": "demo", "aliases": ["demo"],
            "interest_pattern": "[", "turn_guidance": "reply briefly"})
        self.assertTrue(any("regular expression" in item for item in problems))

    def test_unknown_parameter(self):
        problems = validate_config.validate_params({"NOT_A_PARAM": 1})
        self.assertTrue(problems)


if __name__ == "__main__":
    unittest.main()
