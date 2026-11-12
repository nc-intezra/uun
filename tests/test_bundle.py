"""Monthly EoL bundle: build, quality gates, canonical form, signature checks."""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from uun import bundle, safety

NOW = "2026-12-03T04:23:11Z"


def _release(name, eol="2030-01-01", latest=None):
    return {"name": name, "codename": None, "label": name, "releaseDate": "2024-01-01",
            "isLts": False, "ltsFrom": None, "isEol": False, "eolFrom": eol,
            "isMaintained": True, "latest": {"name": latest or f"{name}.1", "date": "2025-01-01",
                                             "link": "https://example.org"}, "custom": None}


def _product(slug, n_releases=2):
    return {"name": slug, "aliases": [], "label": slug.title(), "category": "lang", "tags": [],
            "versionCommand": "x --version", "identifiers": [], "labels": {},
            "links": {"html": f"https://endoflife.date/{slug}"},
            "releases": [_release(f"{i + 1}.0") for i in range(n_releases)]}


def upstream(n=330, extra=(), schema="1.2.0"):
    tracked = sorted(bundle.tracked_slugs())
    slugs = tracked + [f"product-{i:03d}" for i in range(n - len(tracked))] + list(extra)
    items = [_product(s) for s in slugs]
    return {"schema_version": schema, "generated_at": "2026-12-03T00:00:00+00:00",
            "total": len(items), "result": items}


class Build(unittest.TestCase):
    def test_builds_and_normalizes(self):
        up = upstream()
        up["result"][0]["label"] = "Evil\x1b]52;c;cHduZWQ=\x07[@click=app.quit]"
        up["result"][0]["releases"].append({"name": "\x1b[2J", "eolFrom": "2030-01-01"})
        b, warnings = bundle.build_bundle(up, now=NOW)
        self.assertEqual((b["format"], b["schema"], b["month"]), ("uun-eol-bundle", 1, "2026-12"))
        self.assertEqual(len(b["products"]), 330)
        first = b["products"][up["result"][0]["name"]]
        self.assertNotIn("\x1b", first["label"])
        self.assertEqual([r["name"] for r in first["releases"]], ["1.0", "2.0"])
        self.assertEqual(first["releases"][0]["latest"], "1.0.1")
        self.assertNotIn("versionCommand", json.dumps(b))  # allow-list drops everything else
        self.assertEqual(warnings, [])

    def test_canonical_bytes_are_deterministic_and_ascii(self):
        b, _ = bundle.build_bundle(upstream(extra=["café"]), now=NOW)  # invalid slug, dropped
        one, two = bundle.canonical_bytes(b), bundle.canonical_bytes(copy.deepcopy(b))
        self.assertEqual(one, two)
        one.decode("ascii")
        self.assertTrue(one.endswith(b"\n"))
        self.assertEqual(bundle.load_bundle(one)["products"], b["products"])

    def test_missing_tracked_product_is_a_warning_on_first_run(self):
        up = upstream()
        up["result"] = [p for p in up["result"] if p["name"] != "nginx"] + [_product("filler")]
        _, warnings = bundle.build_bundle(up, now=NOW)
        self.assertIn("nginx", warnings[0])


class Gates(unittest.TestCase):
    def assertGate(self, fn, text):
        with self.assertRaises(bundle.BundleError) as cm:
            fn()
        self.assertIn(text, str(cm.exception))

    def test_schema_change_fails(self):
        self.assertGate(lambda: bundle.build_bundle(upstream(schema="2.0.0"), now=NOW), "not 1.x")

    def test_too_few_products_fails(self):
        self.assertGate(lambda: bundle.build_bundle(upstream(n=299), now=NOW), "usable products")

    def test_low_survival_fails(self):
        up = upstream(n=400)                     # 360 usable: above MIN_PRODUCTS,
        for p in up["result"][-40:]:             # but only 90% of upstream survives
            p["releases"] = []
        self.assertGate(lambda: bundle.build_bundle(up, now=NOW), "normalized")

    def test_mass_disappearance_fails(self):
        prev, _ = bundle.build_bundle(upstream(n=400), now="2026-11-03T04:23:11Z")
        self.assertGate(lambda: bundle.build_bundle(upstream(n=330), previous=prev, now=NOW),
                        "disappeared")

    def test_tracked_regression_fails(self):
        prev, _ = bundle.build_bundle(upstream(), now="2026-11-03T04:23:11Z")
        up = upstream()
        up["result"] = [p for p in up["result"] if p["name"] != "python"] + [_product("filler")]
        self.assertGate(lambda: bundle.build_bundle(up, previous=prev, now=NOW), "python")

    def test_not_newer_than_previous_fails(self):
        prev, _ = bundle.build_bundle(upstream(), now=NOW)
        self.assertGate(lambda: bundle.build_bundle(upstream(), previous=prev, now=NOW), "not newer")


class Load(unittest.TestCase):
    def test_rejects_foreign_or_future_formats(self):
        for raw, text in ((b"[]", "not a uun"), (b"{\"format\": \"x\"}", "not a uun"),
                          (b"{\"format\": \"uun-eol-bundle\", \"schema\": 2}", "schema"),
                          (b"not json", "valid JSON")):
            with self.assertRaises(bundle.BundleError) as cm:
                bundle.load_bundle(raw)
            self.assertIn(text, str(cm.exception))

    def test_size_cap(self):
        with self.assertRaises(bundle.BundleError):
            bundle.load_bundle(b" " * (bundle.MAX_BUNDLE_BYTES + 1))


class Cli(unittest.TestCase):
    def test_build_writes_monthly_file(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)
            up = Path(d, "full.json")
            up.write_text(json.dumps(upstream()))
            rc = bundle.main(["build", "--upstream", str(up), "--out", str(Path(d, "dist"))])
            self.assertEqual(rc, 0)
            files = list(Path(d, "dist").glob("uun-eol-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(len(bundle.load_bundle(files[0].read_bytes())["products"]), 330)

    def test_gate_failure_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)
            up = Path(d, "full.json")
            up.write_text(json.dumps(upstream(schema="2.0")))
            self.assertEqual(bundle.main(["build", "--upstream", str(up), "--out", d]), 1)


@unittest.skipUnless(safety.which("ssh-keygen"), "OpenSSH ssh-keygen not installed")
class Signatures(unittest.TestCase):
    """Real ssh-keygen round trips (run in CI; skipped where OpenSSH is absent)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        os.chmod(self.td.name, 0o700)
        self.d = Path(self.td.name)
        self.key = self.d / "k"
        subprocess.run([safety.which("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", "test",
                        "-f", str(self.key)], check=True)
        pub = (self.d / "k.pub").read_text().strip()
        self.allowed = self.d / "allowed"
        self.allowed.write_text(f'{bundle.PRINCIPAL} namespaces="{bundle.NAMESPACE}" {pub}\n')
        self.allowed.chmod(0o600)
        b, _ = bundle.build_bundle(upstream(), now=NOW)
        self.data = bundle.canonical_bytes(b)
        self.file = self.d / "uun-eol-2026-12.json"
        self.file.write_bytes(self.data)

    def tearDown(self):
        self.td.cleanup()

    def sign(self, namespace=bundle.NAMESPACE, key=None):
        subprocess.run([safety.which("ssh-keygen"), "-q", "-Y", "sign", "-f", str(key or self.key),
                        "-n", namespace, str(self.file)], check=True, capture_output=True)
        return Path(f"{self.file}.sig")

    def test_valid_signature(self):
        bundle.verify_signature(self.data, self.sign(), self.allowed)
        self.assertEqual(bundle.main(["verify", str(self.file), "--allowed-signers", str(self.allowed)]), 0)

    def test_tampered_bundle_rejected(self):
        sig = self.sign()
        with self.assertRaises(bundle.BundleError):
            bundle.verify_signature(self.data.replace(b"2030-01-01", b"2099-01-01", 1), sig, self.allowed)

    def test_wrong_namespace_rejected(self):
        sig = self.sign(namespace="file")
        with self.assertRaises(bundle.BundleError):
            bundle.verify_signature(self.data, sig, self.allowed)

    def test_unknown_key_rejected(self):
        other = self.d / "other"
        subprocess.run([safety.which("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-f", str(other)],
                       check=True)
        sig = self.sign(key=other)
        with self.assertRaises(bundle.BundleError):
            bundle.verify_signature(self.data, sig, self.allowed)

    def test_no_key_configured_rejected(self):
        empty = self.d / "empty"
        empty.write_text("# no keys yet\n")
        empty.chmod(0o600)
        with self.assertRaises(bundle.BundleError) as cm:
            bundle.verify_signature(self.data, self.sign(), empty)
        self.assertIn("no signing key", str(cm.exception))

    def test_shipped_signers_refuse_other_keys(self):
        # The allowed-signers file shipped in uun/data must never accept a
        # bundle signed by any other key (here: this test's throwaway key).
        # Before a key is configured it refuses with "no signing key"; after,
        # with "signature check failed". Either way: refused.
        with self.assertRaises(bundle.BundleError) as cm:
            bundle.verify_signature(self.data, self.sign())
        self.assertRegex(str(cm.exception), "no signing key|signature check failed")


if __name__ == "__main__":
    unittest.main()
