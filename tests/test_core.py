"""Unit tests: python3 -m unittest discover -s tests"""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from uun import demo, eol, managers, theme
from uun.config import UnsafeFileError, assert_safe

TODAY = date(2026, 11, 11)


class EolTests(unittest.TestCase):
    def setUp(self):
        self.mysql = demo.cache()["products"]["mysql"]

    def test_clean_version(self):
        self.assertEqual(eol.clean_version("1:10.11.6-0+deb12u1"), "10.11.6")
        self.assertEqual(eol.clean_version("v20.11.1"), "20.11.1")
        self.assertEqual(eol.clean_version("5.15.0-160-generic"), "5.15.0")

    def test_match_longest_prefix(self):
        rels = [{"name": "3"}, {"name": "3.1"}, {"name": "3.10"}]
        self.assertEqual(eol.match_release("3.10.4", rels)["name"], "3.10")
        self.assertEqual(eol.match_release("3.1.2", rels)["name"], "3.1")

    def test_eol_state_and_upgrade(self):
        st = eol.evaluate("mysql", "8.0.43", "x", self.mysql, 180, 60, TODAY)
        self.assertEqual((st.state, st.cycle, st.days_left), ("EOL", "8.0", -195))
        self.assertEqual(st.upgrade_to, "8.4.7")          # prefers LTS over newer innovation
        self.assertTrue(st.suggest_alternative)

    def test_inbound_fix_suppresses_alternatives(self):
        st = eol.evaluate("mysql", "8.0.43", "x", self.mysql, 180, 60, TODAY)
        eol.apply_inbound(st, "8.4.7-0ubuntu1", self.mysql, 180, TODAY)
        self.assertTrue(st.inbound_fixes)
        self.assertFalse(st.suggest_alternative)

    def test_patch_inbound_does_not_fix(self):
        st = eol.evaluate("mysql", "8.0.43", "x", self.mysql, 180, 60, TODAY)
        eol.apply_inbound(st, "8.0.44-0ubuntu0.22.04.1", self.mysql, 180, TODAY)
        self.assertFalse(st.inbound_fixes)

    def test_windows(self):
        p = {"label": "X", "releases": [{"name": "1", "eolFrom": "2027-01-01", "releaseDate": "2020-01-01"}]}
        self.assertEqual(eol.evaluate("x", "1.2", "", p, 180, 60, TODAY).state, "CRIT")
        self.assertEqual(eol.evaluate("x", "1.2", "", p, 180, 30, TODAY).state, "SOON")
        self.assertEqual(eol.evaluate("x", "1.2", "", None, 180, 60, TODAY).state, "UNKNOWN")


class ThemeTests(unittest.TestCase):
    def test_colours(self):
        self.assertEqual(theme.parse_colour("colour196"), "#ff0000")
        self.assertEqual(theme.parse_colour("color244"), "#808080")
        self.assertEqual(theme.parse_colour("brightred"), "#ff0000")
        self.assertIsNone(theme.parse_colour("default"))
        self.assertEqual(theme.parse_style("bg=colour235,fg=#ffffff,bold"),
                         {"bg": "#262626", "fg": "#ffffff", "attrs": ["bold"]})

    def test_bundled_themes_parse_cleanly(self):
        for name in theme.list_themes():
            t = theme.load_theme(name)
            self.assertEqual(t.errors, [], name)
            t.to_textual()

    def test_tmux_syntax(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d, "base.conf")
            base.write_text('set -g @accent colour39\nset -g status-style "bg=red"\n')
            top = Path(d, "t.conf")
            top.write_text("source-file base.conf\nset-option -g pane-border-lines double  # comment\n"
                           "set -ga status-style fg=white\nbind-key R refresh\nunbind-key q\nbogus x\n")
            t = theme.load_theme(str(top))
        self.assertEqual(t.border, "double")
        self.assertEqual(t.style("status-style"), {"bg": "#800000", "fg": "#c0c0c0", "attrs": []})
        self.assertEqual(t.binds, {"R": "refresh"})
        self.assertIn("q", t.unbinds)
        self.assertEqual(len(t.errors), 1)


class ManagerTests(unittest.TestCase):
    def test_sudo_substitution(self):
        cfg = {"apt": {"update": [["@SUDO", "apt-get", "upgrade"]]}}
        m = next(x for x in managers.MANAGERS if x.name == "apt")
        cmds = managers.update_commands(m, cfg, use_sudo=False)
        self.assertEqual(cmds, [["apt-get", "upgrade"]])

    def test_pip_never_touches_system_site(self):
        m = next(x for x in managers.MANAGERS if x.name == "pip")
        os.environ.pop("VIRTUAL_ENV", None)
        cmd = managers.update_commands(m, {}, True, {"rich": "14"})[0]
        self.assertIn("--user", cmd)
        self.assertNotIn("sudo", cmd)


class SafetyTests(unittest.TestCase):
    def test_world_writable_config_refused(self):
        with tempfile.NamedTemporaryFile() as f:
            os.chmod(f.name, 0o666)
            with self.assertRaises(UnsafeFileError):
                assert_safe(Path(f.name))
            os.chmod(f.name, 0o600)
            assert_safe(Path(f.name))


if __name__ == "__main__":
    unittest.main()
