"""Offline EoL data: importing signed bundles, rollback protection, the
packaged baseline, GitHub bundle refresh, staleness reporting and the CLI."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_bundle import upstream

from uun import bundle, eol, safety, scan
from uun.__main__ import main
from uun.config import Config, ConfigError, load_config


def make_bundle(d: Path, now: str) -> Path:
    b, _ = bundle.build_bundle(upstream(), now=now)
    f = d / f"uun-eol-{b['month']}.json"
    f.write_bytes(bundle.canonical_bytes(b))
    Path(f"{f}.sig").write_text("-----BEGIN SSH SIGNATURE-----\nfake\n-----END SSH SIGNATURE-----\n")
    return f


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        os.chmod(self.td.name, 0o700)
        self.d = Path(self.td.name)
        self.cache = self.d / "cache" / "eol.json"
        p = mock.patch("uun.bundle.verify_signature")   # real signatures: test_bundle / ImportSigned
        self.verify = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.td.cleanup)


class Import(Base):
    def test_import_into_empty_cache(self):
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        status, b = bundle.import_bundle(f, cache=self.cache)
        self.assertEqual(status, "imported")
        self.verify.assert_called_once()
        data = eol.load_cache(self.cache)
        self.assertEqual(data["source"], "bundle")
        self.assertEqual(data["bundle"], {"month": "2026-11", "generated_at": "2026-11-03T04:23:11Z"})
        self.assertEqual(data["as_of"], bundle.iso_epoch("2026-11-03T04:23:11Z"))
        self.assertEqual(len(data["products"]), 330)
        self.assertTrue(eol.cache_info(self.cache)["label"].startswith("bundle 2026-11, "))

    def test_same_bundle_twice_is_a_noop(self):
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        bundle.import_bundle(f, cache=self.cache)
        before = self.cache.stat().st_mtime_ns
        self.assertEqual(bundle.import_bundle(f, cache=self.cache)[0], "current")
        self.assertEqual(self.cache.stat().st_mtime_ns, before)

    def test_rollback_refused_unless_allowed(self):
        new = make_bundle(self.d, "2026-12-03T04:23:11Z")
        (self.d / "old").mkdir()
        old = make_bundle(self.d / "old", "2026-11-03T04:23:11Z")
        bundle.import_bundle(new, cache=self.cache)
        with self.assertRaisesRegex(bundle.BundleError, "already has newer data.*--allow-older"):
            bundle.import_bundle(old, cache=self.cache)
        self.assertEqual(eol.load_cache(self.cache)["bundle"]["month"], "2026-12")
        bundle.import_bundle(old, cache=self.cache, allow_older=True)
        self.assertEqual(eol.load_cache(self.cache)["bundle"]["month"], "2026-11")

    def test_newer_live_data_wins(self):
        eol.write_cache({"fetched_at": time.time(), "as_of": time.time(), "source": "api",
                         "products": {"python": {"label": "Python", "releases": []}}, "errors": {}},
                        self.cache)
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        with self.assertRaisesRegex(bundle.BundleError, "live endoflife.date data"):
            bundle.import_bundle(f, cache=self.cache)

    def test_bad_signature_leaves_cache_untouched(self):
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        self.verify.side_effect = bundle.BundleError("signature check failed")
        with self.assertRaisesRegex(bundle.BundleError, "signature"):
            bundle.import_bundle(f, cache=self.cache)
        self.assertFalse(self.cache.exists())

    def test_missing_signature_and_oversize(self):
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        Path(f"{f}.sig").unlink()
        with self.assertRaisesRegex(bundle.BundleError, "signature .* not found"):
            bundle.import_bundle(f, cache=self.cache)
        big = self.d / "big.json"
        with open(big, "wb") as fh:
            fh.truncate(bundle.MAX_BUNDLE_BYTES + 1)
        with self.assertRaisesRegex(bundle.BundleError, "larger than"):
            bundle.import_bundle(big, cache=self.cache)

    def test_invalid_bundle_rejected_after_signature(self):
        f = self.d / "uun-eol-2026-11.json"
        f.write_text(json.dumps({"format": "something-else"}))
        Path(f"{f}.sig").write_text("x")
        with self.assertRaises(bundle.BundleError):
            bundle.import_bundle(f, cache=self.cache)
        self.assertFalse(self.cache.exists())

    def test_cache_metadata_is_sanitized(self):
        self.cache.parent.mkdir()
        self.cache.write_text(json.dumps({
            "fetched_at": 1, "as_of": "nope", "source": "\x1b[31mevil", "products": {},
            "bundle": {"month": "\x1b]52;c;x\x07" * 5}}))
        data = eol.load_cache(self.cache)
        self.assertEqual((data["source"], data["as_of"]), ("api", 1))
        self.assertNotIn("bundle", data)
        self.cache.write_text(json.dumps({"fetched_at": 1, "source": "bundle", "products": {},
                                          "bundle": {"month": "\x1b]52;c;x\x07 2026-11"}}))
        self.assertNotIn("\x1b", json.dumps(eol.load_cache(self.cache)))


class Baseline(Base):
    def test_imports_when_cache_empty_then_stays_quiet(self):
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        self.assertIn("bundled snapshot 2026-11", bundle.ensure_baseline(self.cache, f))
        self.assertIsNone(bundle.ensure_baseline(self.cache, f))

    def test_newer_cache_is_kept(self):
        eol.write_cache({"fetched_at": time.time(), "as_of": time.time(), "source": "api",
                         "products": {"python": {"label": "Python", "releases": []}}, "errors": {}},
                        self.cache)
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        self.assertIsNone(bundle.ensure_baseline(self.cache, f))
        self.assertEqual(eol.load_cache(self.cache)["source"], "api")

    def test_no_baseline_and_broken_baseline(self):
        self.assertIsNone(bundle.ensure_baseline(self.cache, self.d / "absent.json"))
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        self.verify.side_effect = bundle.BundleError("signature check failed")
        self.assertIn("not used: signature", bundle.ensure_baseline(self.cache, f))
        self.assertFalse(self.cache.exists())


class FromGitHub(Base):
    def fake_download(self, files: dict):
        def dl(url, limit):
            for suffix, body in files.items():
                if url.endswith(suffix):
                    return body
            raise bundle.BundleError(f"{url}: HTTP 404")
        return mock.patch("uun.bundle._download", side_effect=dl)

    def releases(self, *tags, draft=()):
        return json.dumps([{"tag_name": t, "draft": t in draft} for t in tags]).encode()

    def test_picks_newest_eol_release_and_imports(self):
        f = make_bundle(self.d, "2026-12-03T04:23:11Z")
        files = {"releases?per_page=50": self.releases("v0.1.1", "eol-2026-11", "eol-2026-12",
                                                       "eol-2027-01", "eol-9999-99x", draft=("eol-2027-01",)),
                 "/eol-2026-12/uun-eol-2026-12.json": f.read_bytes(),
                 "/eol-2026-12/uun-eol-2026-12.json.sig": b"sig"}
        with self.fake_download(files):
            msg = bundle.refresh_from_github("nc-intezra/uun", self.cache)
            self.assertIn("imported signed bundle 2026-12", msg)
            first = eol.load_cache(self.cache)["fetched_at"]
            time.sleep(0.01)
            self.assertIn("already up to date", bundle.refresh_from_github("nc-intezra/uun", self.cache))
            self.assertGreater(eol.load_cache(self.cache)["fetched_at"], first)

    def test_failures(self):
        with self.assertRaisesRegex(bundle.BundleError, "invalid eol_bundle_repo"):
            bundle.refresh_from_github("../../etc", self.cache)
        with self.fake_download({"releases?per_page=50": self.releases("v0.1.1")}):
            with self.assertRaisesRegex(bundle.BundleError, "no eol-YYYY-MM releases"):
                bundle.refresh_from_github("nc-intezra/uun", self.cache)
        with self.fake_download({"releases?per_page=50": self.releases("eol-2026-12")}):
            with self.assertRaisesRegex(bundle.BundleError, "404"):
                bundle.refresh_from_github("nc-intezra/uun", self.cache)
        self.assertFalse(self.cache.exists())

    def test_download_size_cap(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(eol._OPENER, "open", return_value=Resp(b"x" * 300_000)):
            with self.assertRaisesRegex(bundle.BundleError, "larger than"):
                bundle._download("https://example.org/x", 100_000)


class Sources(Base):
    def cfg(self, **kw):
        c = Config(**kw)
        c.products_file = c.alternatives_file = None
        return c

    def test_offline_never_touches_network(self):
        with mock.patch("uun.eol.refresh") as api, mock.patch("uun.bundle.refresh_from_github") as gh, \
                mock.patch("uun.bundle.ensure_baseline", return_value=None):
            res = scan.refresh_eol(self.cfg(eol_source="offline"), self.cache)
        api.assert_not_called()
        gh.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertIn("offline mode", res["messages"][0])
        self.assertFalse(scan.eol_refresh_due(self.cfg(eol_source="offline"), self.cache))

    def test_bundle_source_uses_github(self):
        with mock.patch("uun.bundle.refresh_from_github", return_value="EoL data: ok") as gh, \
                mock.patch("uun.bundle.ensure_baseline", return_value=None):
            res = scan.refresh_eol(self.cfg(eol_source="bundle", eol_bundle_repo="a/b"), self.cache)
        gh.assert_called_once_with("a/b", self.cache)
        self.assertEqual((res["ok"], res["messages"]), (True, ["EoL data: ok"]))

    def test_bundle_due_by_last_check_not_data_age(self):
        f = make_bundle(self.d, "2026-01-03T04:23:11Z")      # data months old ...
        bundle.import_bundle(f, cache=self.cache)            # ... but checked just now
        self.assertFalse(scan.eol_refresh_due(self.cfg(eol_source="bundle"), self.cache))
        self.assertTrue(scan.eol_refresh_due(self.cfg(eol_source="api"), self.cache))
        label, stale = scan.data_status(self.cfg(stale_days=45), self.cache)
        self.assertTrue(stale)
        self.assertIn("bundle 2026-01", label)


class ConfigValidation(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(text)
            p.chmod(0o600)
            return load_config(p)

    def test_values(self):
        self.assertEqual(self.load('[general]\neol_source = "bundle"\n').eol_source, "bundle")
        for bad in ('eol_source = "ftp"', 'eol_bundle_repo = "x/../../y z"', "stale_days = 0"):
            with self.assertRaises(ConfigError, msg=bad):
                self.load(f"[general]\n{bad}\n")


class Cli(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("uun.eol.EOL_CACHE", self.cache), \
                mock.patch("uun.bundle.import_bundle.__defaults__",
                           (None, None, False, self.cache)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_import_eol(self):
        f = make_bundle(self.d, "2026-11-03T04:23:11Z")
        code, out, _ = self.run_cli("--import-eol", str(f))
        self.assertEqual(code, 0)
        self.assertIn("imported EoL bundle 2026-11 (330 products", out)
        self.assertIn("already in use", self.run_cli("--import-eol", str(f))[1])
        (self.d / "old").mkdir()
        old = make_bundle(self.d / "old", "2026-10-03T04:23:11Z")
        code, _, err = self.run_cli("--import-eol", str(old))
        self.assertEqual(code, 1)
        self.assertIn("--allow-older", err)
        self.assertEqual(self.run_cli("--import-eol", str(old), "--allow-older")[0], 0)
        sig = self.d / "elsewhere.sig"
        sig.write_text("x")
        self.run_cli("--import-eol", str(f), "--sig", str(sig))
        self.assertEqual(self.verify.call_args.args[1], sig)


@unittest.skipUnless(safety.which("ssh-keygen"), "OpenSSH not installed")
class ImportSigned(unittest.TestCase):
    """End to end with real ssh-keygen signatures (runs in CI)."""

    def test_signed_import_and_tamper(self):
        with tempfile.TemporaryDirectory() as td:
            os.chmod(td, 0o700)
            d = Path(td)
            key = d / "k"
            subprocess.run([safety.which("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", "t",
                            "-f", str(key)], check=True)
            allowed = d / "allowed"
            allowed.write_text(f'{bundle.PRINCIPAL} namespaces="{bundle.NAMESPACE}" '
                               f'{(d / "k.pub").read_text().strip()}\n')
            allowed.chmod(0o600)
            f = make_bundle(d, "2026-11-03T04:23:11Z")
            subprocess.run([safety.which("ssh-keygen"), "-q", "-Y", "sign", "-f", str(key),
                            "-n", bundle.NAMESPACE, str(f)], check=True, capture_output=True)
            cache = d / "c" / "eol.json"
            tampered = d / "t" / f.name
            tampered.parent.mkdir()
            tampered.write_bytes(f.read_bytes().replace(b'"2030-01-01"', b'"2099-01-01"', 1))
            Path(f"{tampered}.sig").write_bytes(Path(f"{f}.sig").read_bytes())
            with self.assertRaises(bundle.BundleError):
                bundle.import_bundle(tampered, allowed_signers=allowed, cache=cache)
            self.assertFalse(cache.exists())
            self.assertEqual(bundle.import_bundle(f, allowed_signers=allowed, cache=cache)[0], "imported")
            # the shipped key must refuse a bundle signed by anyone else
            with self.assertRaises(bundle.BundleError):
                bundle.import_bundle(f, cache=d / "c2" / "eol.json")


if __name__ == "__main__":
    unittest.main()
