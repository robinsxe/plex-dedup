"""
Regression tests for DedupEngine.execute_all selection handling — the only
code path that deletes media. An empty plan list must run NOTHING; passing
None means "use the full scan result".

Run: python -m unittest test_dedup_engine
"""

import sys
import unittest
from unittest.mock import MagicMock

import test_stubs

test_stubs.install_common_stubs()

# dedup_engine imports these extra names from plex_client; the shared stub only
# provides PlexClient, so add the value classes it also pulls in.
_plex_stub = sys.modules["plex_client"]
_plex_stub.DuplicateGroup = type("DuplicateGroup", (), {})
_plex_stub.MediaFile = type("MediaFile", (), {})

from dedup_engine import DedupEngine  # noqa: E402


def _make_engine(dry_run: bool = True) -> DedupEngine:
    """Build an engine without touching __init__ (which wires real clients)."""
    e = DedupEngine.__new__(DedupEngine)
    e.config = MagicMock()
    e.config.dry_run = dry_run
    e.plex = MagicMock()
    e._plans = []
    return e


class ExecuteAllSelectionTests(unittest.TestCase):
    def test_empty_list_runs_nothing(self):
        """An empty selection must NOT fall back to every plan."""
        e = _make_engine()
        e._plans = ["PLAN_A", "PLAN_B"]
        calls = []
        e.execute_plan = lambda plan: (calls.append(plan) or True)

        result = e.execute_all([])

        self.assertEqual(calls, [], "execute_all([]) must not execute any plan")
        self.assertEqual(result, {"success": 0, "failed": 0})

    def test_none_uses_scan_result(self):
        """None means 'no argument given' — use the stored scan result."""
        e = _make_engine()
        e._plans = ["PLAN_A", "PLAN_B"]
        calls = []
        e.execute_plan = lambda plan: (calls.append(plan) or True)

        result = e.execute_all(None)

        self.assertEqual(calls, ["PLAN_A", "PLAN_B"])
        self.assertEqual(result["success"], 2)

    def test_explicit_subset_runs_only_that_subset(self):
        e = _make_engine()
        e._plans = ["PLAN_A", "PLAN_B", "PLAN_C"]
        calls = []
        e.execute_plan = lambda plan: (calls.append(plan) or True)

        e.execute_all(["PLAN_B"])

        self.assertEqual(calls, ["PLAN_B"])


if __name__ == "__main__":
    unittest.main()
