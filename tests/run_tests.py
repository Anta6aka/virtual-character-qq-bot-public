"""Run isolated tests without importing AstrBot against a production data directory."""

import os
import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
scratch = Path(tempfile.gettempdir()) / "virtual-role-tests"
scratch.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(scratch)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--pattern',default='test_*.py',help='unittest discovery filename pattern')
args=parser.parse_args()
with tempfile.TemporaryDirectory(prefix="virtual-role-tests-") as isolated:
    os.environ["ASTRBOT_ROOT"] = isolated
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
