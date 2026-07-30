"""
Tests for config.py — boolean env parsing (_parse_bool) and the
settings.json > env > default precedence chain.

config.py needs python-dotenv (not installed in the stub test environment) and
test_stubs registers a bare stub under the name "config" for other test
modules. So the real module is loaded from file under the separate name
"config_real", with a no-op dotenv stub, and SETTINGS_FILE pointed at a temp
dir BEFORE load (the module freezes that path at import time).

Run: python -m unittest test_config
"""

import importlib.util
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

if "dotenv" not in sys.modules:
    _dotenv_stub = types.ModuleType("dotenv")
    _dotenv_stub.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = _dotenv_stub

_TMP = tempfile.TemporaryDirectory()
_SETTINGS_PATH = os.path.join(_TMP.name, "settings.json")
# config.py freezes SETTINGS_FILE (and the default args derived from it) at
# import time, so the env var only needs to point at the temp dir during the
# module load below — restore it afterwards to keep the process env clean for
# any other test that loads the real config module.
_orig_settings_env = os.environ.get("SETTINGS_FILE")
os.environ["SETTINGS_FILE"] = _SETTINGS_PATH

_spec = importlib.util.spec_from_file_location(
    "config_real", os.path.join(os.path.dirname(__file__), "config.py")
)
config_real = importlib.util.module_from_spec(_spec)
sys.modules["config_real"] = config_real
_spec.loader.exec_module(config_real)

if _orig_settings_env is None:
    os.environ.pop("SETTINGS_FILE", None)
else:
    os.environ["SETTINGS_FILE"] = _orig_settings_env


class _SettingsIsolatedCase(unittest.TestCase):
    def setUp(self):
        if os.path.exists(_SETTINGS_PATH):
            os.remove(_SETTINGS_PATH)

    def _write_settings(self, data: dict):
        with open(_SETTINGS_PATH, "w") as f:
            json.dump(data, f)


class BoolParsingTests(_SettingsIsolatedCase):
    def _dry_run(self, raw: str) -> bool:
        with patch.dict(os.environ, {"DRY_RUN": raw}):
            return config_real.Config.from_env().dry_run

    def test_truthy_variants(self):
        """DRY_RUN=1/yes/on must mean dry-run, not silently go LIVE."""
        for raw in ("true", "TRUE", "True", "1", "yes", "on", "YES", " true "):
            self.assertTrue(self._dry_run(raw), f"DRY_RUN={raw!r}")

    def test_falsy_variants(self):
        for raw in ("false", "FALSE", "0", "no", "off", "Off"):
            self.assertFalse(self._dry_run(raw), f"DRY_RUN={raw!r}")

    def test_unrecognized_value_keeps_safe_default_and_warns(self):
        with self.assertLogs("config_real", level="WARNING") as cm:
            self.assertTrue(self._dry_run("maybe"))
        self.assertIn("dry_run", "\n".join(cm.output))

    def test_unrecognized_value_keeps_false_default(self):
        """A garbage value must not accidentally enable an opt-in flag."""
        with patch.dict(os.environ, {"WEB_AUTH_DISABLED": "maybe"}):
            with self.assertLogs("config_real", level="WARNING"):
                cfg = config_real.Config.from_env()
        self.assertFalse(cfg.web_auth_disabled)


class SettingsPrecedenceTests(_SettingsIsolatedCase):
    def test_settings_file_beats_env(self):
        self._write_settings({"plex_url": "http://from-settings:32400"})
        with patch.dict(os.environ, {"PLEX_URL": "http://from-env:32400"}):
            cfg = config_real.Config.from_env()
        self.assertEqual(cfg.plex_url, "http://from-settings:32400")

    def test_empty_settings_value_falls_back_to_env(self):
        self._write_settings({"plex_url": ""})
        with patch.dict(os.environ, {"PLEX_URL": "http://from-env:32400"}):
            cfg = config_real.Config.from_env()
        self.assertEqual(cfg.plex_url, "http://from-env:32400")

    def test_json_true_beats_env_false(self):
        self._write_settings({"dry_run": True})
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            cfg = config_real.Config.from_env()
        self.assertTrue(cfg.dry_run)

    def test_json_false_beats_env_true(self):
        self._write_settings({"dry_run": False})
        with patch.dict(os.environ, {"DRY_RUN": "true"}):
            cfg = config_real.Config.from_env()
        self.assertFalse(cfg.dry_run)


class SaveToFileTests(_SettingsIsolatedCase):
    def test_web_and_schedule_fields_never_persisted(self):
        cfg = config_real.Config(web_password="not-a-real-secret",
                                 web_auth_disabled=True)
        self.assertTrue(cfg.save_to_file(_SETTINGS_PATH))
        with open(_SETTINGS_PATH) as f:
            data = json.load(f)
        for key in ("web_password", "web_auth_disabled", "web_host",
                    "web_port", "schedule_enabled"):
            self.assertNotIn(key, data)

    def test_file_mode_is_0600(self):
        config_real.Config().save_to_file(_SETTINGS_PATH)
        mode = stat.S_IMODE(os.stat(_SETTINGS_PATH).st_mode)
        self.assertEqual(mode, 0o600)

    def test_saved_values_load_back_via_from_env(self):
        cfg = config_real.Config(keep_strategy="newest", plex_token="tok123")
        cfg.save_to_file(_SETTINGS_PATH)
        loaded = config_real.Config.from_env()
        self.assertEqual(loaded.keep_strategy, "newest")
        self.assertEqual(loaded.plex_token, "tok123")


if __name__ == "__main__":
    unittest.main()
