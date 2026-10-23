"""Relationship parameter overrides: unknown keys and unsafe values fail closed."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARAMS = ROOT / "AstrBot/data/plugins/astrbot_plugin_ltm_fts/relationship/params.py"
spec = importlib.util.spec_from_file_location("relationship_params_test", PARAMS)
params = importlib.util.module_from_spec(spec)
spec.loader.exec_module(params)


class RelationshipParamsTests(unittest.TestCase):
    def setUp(self):
        self.original = {key: getattr(params, key) for key in params._OVERRIDE_KEYS}

    def tearDown(self):
        for key, value in self.original.items():
            setattr(params, key, value)

    def test_apply_override(self):
        applied = params.apply_overrides({"F_MAX": 50.0, "TRUSTED_EPISODES": 3})
        self.assertEqual(applied, 2)
        self.assertEqual(params.F_MAX, 50.0)
        self.assertEqual(params.TRUSTED_EPISODES, 3)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValueError):
            params.apply_overrides({"NOT_A_PARAM": 1})

    def test_unsafe_value_is_rejected(self):
        with self.assertRaises(ValueError):
            params.apply_overrides({"F_MAX": 0})
        with self.assertRaises(ValueError):
            params.apply_overrides({"F_DAILY_POSITIVE_CAP": -1})

    def test_load_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relationship_params.json"
            path.write_text(json.dumps({"F_MAX": 42.0}), encoding="utf-8")
            self.assertEqual(params.load_overrides(path), 1)
            self.assertEqual(params.F_MAX, 42.0)
            self.assertEqual(params.load_overrides(Path(directory) / "absent.json"), 0)

    def test_invalid_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relationship_params.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError):
                params.load_overrides(path)


if __name__ == "__main__":
    unittest.main()
