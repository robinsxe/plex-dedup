"""
Tests for crash-safe JSON persistence (state_io).

Run: python -m unittest test_state_io
"""

import json
import os
import tempfile
import unittest

from state_io import atomic_write_json, load_json


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def test_write_then_read_roundtrip(self):
        atomic_write_json(self.path, {"a": 1, "b": [2, 3]})
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"a": 1, "b": [2, 3]})

    def test_no_temp_files_left_behind(self):
        atomic_write_json(self.path, {"x": 1})
        leftovers = [n for n in os.listdir(self.dir) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_failed_write_leaves_original_intact(self):
        atomic_write_json(self.path, {"good": True})
        # A set is not JSON-serializable -> json.dump raises mid-write.
        with self.assertRaises(TypeError):
            atomic_write_json(self.path, {"bad": {1, 2, 3}})
        # Original content must survive, and no temp file left behind.
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"good": True})
        self.assertEqual(
            [n for n in os.listdir(self.dir) if n.startswith(".tmp-")], []
        )

    def test_creates_missing_directory(self):
        nested = os.path.join(self.dir, "sub", "dir", "state.json")
        atomic_write_json(nested, {"ok": 1})
        self.assertTrue(os.path.exists(nested))


class LoadJsonTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def test_missing_returns_default(self):
        self.assertEqual(load_json(self.path), {})
        self.assertEqual(load_json(self.path, default=[]), [])

    def test_valid_loads(self):
        atomic_write_json(self.path, {"k": "v"})
        self.assertEqual(load_json(self.path), {"k": "v"})

    def test_corrupt_is_quarantined_and_default_returned(self):
        with open(self.path, "w") as f:
            f.write("{ this is not valid json ")
        result = load_json(self.path, default={})
        self.assertEqual(result, {})
        # Original path is gone; a quarantine copy exists.
        self.assertFalse(os.path.exists(self.path))
        quarantined = [
            n for n in os.listdir(self.dir)
            if n.startswith("state.json.corrupt-")
        ]
        self.assertEqual(len(quarantined), 1)


if __name__ == "__main__":
    unittest.main()
