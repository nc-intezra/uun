"""Unreadable / unwritable XDG locations must give clear errors, not tracebacks."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uun import __main__ as cli
from uun import config, eol, theme


def _denied(*a, **k):
    raise PermissionError(13, "Permission denied")


class UnreadableConfigDir(unittest.TestCase):
    def run_cli(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(list(args))
        return rc, err.getvalue()

    def test_report_with_foreign_xdg_config_home(self):
        foreign = "/home/someone-else/.config"
        cfg = Path(foreign) / "uun" / "config.toml"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": foreign}), \
                mock.patch.object(config, "CONFIG_FILE", cfg), \
                mock.patch.object(Path, "exists", _denied):
            rc, err = self.run_cli("--report")
        self.assertEqual(rc, 3)
        self.assertIn("cannot access", err)
        self.assertIn("Permission denied", err)
        self.assertIn("XDG_CONFIG_HOME", err)
        self.assertNotIn("Traceback", err)

    def test_unreadable_config_is_fatal_not_ignored(self):
        # silently falling back to defaults could drop e.g. use_sudo = false
        with mock.patch.object(Path, "exists", _denied):
            with self.assertRaises(config.ConfigError):
                config.load_config(Path("/somewhere/config.toml"))

    def test_invalid_toml_is_one_clear_line(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)
            p = Path(d, "config.toml")
            p.write_text("[general\n")
            p.chmod(0o600)
            rc, err = self.run_cli("--config", str(p), "--report")
        self.assertEqual(rc, 3)
        self.assertIn("cannot load configuration", err)
        self.assertNotIn("Traceback", err)


class UnreadableThemeDir(unittest.TestCase):
    def test_list_and_find_skip_unreadable_dirs(self):
        bad = Path("/nonexistent-private/themes")
        real = config.data_path("themes")
        orig_exists, orig_is_dir = Path.exists, Path.is_dir

        def exists(self, *a, **k):
            return _denied() if str(self).startswith(str(bad)) else orig_exists(self, *a, **k)

        def is_dir(self, *a, **k):
            return _denied() if str(self).startswith(str(bad)) else orig_is_dir(self, *a, **k)
        with mock.patch.object(theme, "theme_dirs", lambda: [bad, real]), \
                mock.patch.object(Path, "exists", exists), mock.patch.object(Path, "is_dir", is_dir):
            self.assertIn("nord", theme.list_themes())
            self.assertEqual(theme.find_theme("nord"), real / "nord.conf")
            self.assertEqual(theme.load_theme("nord").errors, [])


class UnwritableCache(unittest.TestCase):
    def test_refresh_reports_instead_of_crashing(self):
        product = {"label": "X", "releases": [{"name": "1"}]}
        with mock.patch.object(eol, "_fetch_one", lambda api, s, t: (s, product, None)), \
                mock.patch("tempfile.mkstemp", _denied), \
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/home/someone-else/.cache"}):
            res = eol.refresh("https://example.invalid/api", ["python"],
                              cache=Path("/home/someone-else/.cache/uun/eol.json"))
        self.assertIn("python", res["products"])          # data kept for this session
        self.assertIn("cannot write EoL cache", res["cache_error"])
        self.assertIn("XDG_CACHE_HOME", res["cache_error"])

    def test_refresh_eol_cli_exit_code(self):
        with mock.patch.object(eol, "refresh", lambda *a, **k: {"products": {"x": {}}, "errors": {},
                                                                "cache_error": "cannot write EoL cache /c"}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = cli.main(["--refresh-eol"])
        self.assertEqual(rc, 1)
        self.assertIn("cannot write EoL cache", err.getvalue())


if __name__ == "__main__":
    unittest.main()
