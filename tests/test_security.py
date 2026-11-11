"""Regression tests for the 2026-11 security audit (see SECURITY.md).

Each test reproduces the original proof of concept and asserts it no longer works.
Run: python3 -m unittest discover -s tests
"""
import asyncio
import io
import os
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from uun import demo, detect, eol, managers, safety
from uun.safety import UnsafeFileError

REPO = Path(__file__).resolve().parent.parent


class _Tmp:
    """Private temp dir (0700) so the sticky world-writable /tmp above it is allowed."""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        os.chmod(self.td.name, 0o700)
        return Path(self.td.name)

    def __exit__(self, *a):
        self.td.cleanup()


class UUN01_CwdCodeExecution(unittest.TestCase):
    """Probes/checks must not import or read config from the launch directory."""

    def test_detect_does_not_import_from_cwd(self):
        with _Tmp() as d:
            marker = d / "PWNED"
            (d / "django.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nget_version=lambda:'9'\n")
            (d / "pip").mkdir()
            (d / "pip" / "__main__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
            old = os.getcwd()
            os.chdir(d)
            try:
                detect.detect(detect.load_specs(REPO / "uun/data/products.toml"))
                managers.check_pip()
            finally:
                os.chdir(old)
            self.assertFalse(marker.exists(), "code from the working directory was executed")

    def test_children_run_in_safe_cwd(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["cwd"] = kw.get("cwd")
            raise OSError("stop")
        with mock.patch("subprocess.run", fake_run):
            managers.run(["sh", "-c", "true"])
        self.assertEqual(seen["cwd"], "/")


class UUN02_PathHijack(unittest.TestCase):
    def test_relative_and_writable_path_entries_ignored(self):
        with _Tmp() as d:
            ww = d / "ww"
            ww.mkdir()
            os.chmod(ww, 0o777)
            for where in (d, ww):
                exe = where / "apt"
                exe.write_text("#!/bin/sh\necho hijacked\n")
                exe.chmod(0o755)
            old = os.getcwd()
            os.chdir(d)
            try:
                dirs = safety.trusted_dirs(f".:{ww}::relative/bin:/usr/bin")
                self.assertNotIn(".", dirs)
                self.assertNotIn(str(ww), dirs)
                self.assertNotIn("", dirs)
                self.assertNotIn("relative/bin", dirs)
                found = safety.which("apt", dirs)
                self.assertTrue(found is None or found.startswith("/usr/"), found)
            finally:
                os.chdir(old)

    def test_sudo_must_be_root_owned_setuid(self):
        p = safety.sudo_path()
        if p is None:
            self.skipTest("no sudo on this host")
        st = os.stat(p)
        self.assertEqual(st.st_uid, 0)
        self.assertTrue(st.st_mode & stat.S_ISUID or "/run/wrappers/" in p)


class UUN03_ConfigToctou(unittest.TestCase):
    def test_writable_parent_dir_refused(self):
        with _Tmp() as d:
            cfgdir = d / "uun"
            cfgdir.mkdir()
            f = cfgdir / "config.toml"
            f.write_text("[general]\n")
            f.chmod(0o600)
            safety.read_trusted(f)
            cfgdir.chmod(0o777)
            with self.assertRaises(UnsafeFileError):
                safety.read_trusted(f)

    def test_symlink_to_unsafe_target_refused(self):
        with _Tmp() as d:
            bad = d / "bad.toml"
            bad.write_text("x = 1\n")
            bad.chmod(0o666)
            link = d / "config.toml"
            link.symlink_to(bad)
            with self.assertRaises(UnsafeFileError):
                safety.read_trusted(link)

    def test_validation_uses_open_descriptor(self):
        # the file is checked via fstat on the same fd it is read from
        with _Tmp() as d:
            f = d / "c.toml"
            f.write_text("a = 1\n")
            f.chmod(0o600)
            with mock.patch("os.fstat", wraps=os.fstat) as fst:
                safety.read_trusted(f)
                self.assertTrue(fst.called)


class UUN04_Injection(unittest.TestCase):
    EVIL = {"label": "Evil\x1b]52;c;cHduZWQ=\x07\x1b[2J\u202e",
            "links": {"html": "javascript:alert(1)"},
            "releases": [{"name": "1", "eolFrom": "2020-01-01\x1b[31m", "releaseDate": "2019-01-01",
                          "latest": {"name": "[@click=app.update_all]9.9[/]"}},
                         {"name": "\x1b[2J", "eolFrom": "2030-01-01"}]}

    def test_normalize_strips_controls_and_rejects_bad_fields(self):
        p = eol.normalize_product(self.EVIL, "x")
        self.assertNotIn("\x1b", p["label"])
        self.assertNotIn("\u202e", p["label"])
        self.assertIsNone(p["html"])
        self.assertEqual([r["name"] for r in p["releases"]], ["1"])
        self.assertIsNone(p["releases"][0]["latest"])     # markup-shaped version rejected
        self.assertIsNone(p["releases"][0]["eolFrom"])    # malformed date rejected

    def test_poisoned_cache_is_sanitised(self):
        with _Tmp() as d:
            c = d / "eol.json"
            import json
            c.write_text(json.dumps({"fetched_at": 1, "products": {"x": self.EVIL, "../bad": {}}}))
            c.chmod(0o600)
            data = eol.load_cache(c)
            self.assertEqual(list(data["products"]), ["x"])
            self.assertNotIn("\x1b", json.dumps(data))

    def test_corrupt_cache_does_not_crash(self):
        with _Tmp() as d:
            c = d / "eol.json"
            c.write_text('{"products": {"x": {"releases": [1, null, {"name": 5}]}}}')
            c.chmod(0o600)
            data = eol.load_cache(c)
            st = eol.evaluate("x", "1.0", "t", data["products"].get("x"), 180, 60)
            self.assertEqual(st.state, "UNKNOWN")

    def test_tui_cells_render_brackets_literally(self):
        from textual.widgets._data_table import default_cell_formatter
        from uun.app import UunApp
        from uun.config import load_config
        cache = demo.cache()
        cache["products"]["redis"]["releases"][0]["latest"] = "[@click=app.update_all]9.9[/]"

        async def run():
            with mock.patch.object(demo, "cache", lambda: cache):
                app = UunApp(load_config(), demo=True)
                async with app.run_test(size=(160, 40)) as pilot:
                    await pilot.pause(0.3)
                    t = app.query_one("#eol")
                    for i in range(t.row_count):
                        for cell in t.get_row_at(i):
                            out = default_cell_formatter(cell)
                            self.assertNotIsInstance(cell, str, "str cells are parsed as markup")
                            self.assertNotIn("@click", getattr(out, "plain", ""))
        asyncio.run(run())

    def test_report_output_has_no_escape_sequences(self):
        st = eol.evaluate("x", "1.0\x1b[2J", "src\x1b]0;t\x07", self.EVIL, 180, 60)
        self.assertFalse(any("\x1b" in str(v) for v in st.__dict__.values()))


class UUN05_DependencyFloors(unittest.TestCase):
    def test_pyproject_pins_patched_versions(self):
        text = (REPO / "pyproject.toml").read_text()
        for req in ('"pygments>=2.20.0"', '"markdown-it-py>=2.2.0"'):
            self.assertIn(req, text)


class UUN06_RedirectDowngrade(unittest.TestCase):
    def test_http_redirect_refused(self):
        h = eol._HttpsOnlyRedirect()
        req = mock.Mock()
        with self.assertRaises(urllib.error.HTTPError):
            h.redirect_request(req, io.BytesIO(), 302, "Found", {}, "http://evil.example/x")


class UUN07_PipOptionInjection(unittest.TestCase):
    def test_option_like_names_dropped(self):
        m = next(x for x in managers.MANAGERS if x.name == "pip")
        cmds = managers.update_commands(m, {}, True, {"--index-url=https://evil/": "1", "rich": "14"})
        argv = cmds[0]
        self.assertNotIn("--index-url=https://evil/", argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["rich"])


class UUN09_ThemeRead(unittest.TestCase):
    def test_device_files_not_read(self):
        from uun import theme
        with _Tmp() as d:
            t = d / "t.conf"
            t.write_text("source-file /dev/zero\n")
            th = theme.load_theme(str(t))
            self.assertTrue(any("regular file" in e for e in th.errors))


if __name__ == "__main__":
    unittest.main()
