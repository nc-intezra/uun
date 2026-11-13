"""Signed offline EoL data bundles (design: "uun: offline EoL data bundles").

A bundle is a monthly snapshot of endoflife.date's /api/v1/products/full,
normalized with the same allow-list uun applies to live API data, written as
canonical JSON and signed in CI with `ssh-keygen -Y sign` (namespace
"uun-eol"). uun verifies bundles offline with `ssh-keygen -Y verify` against
the allowed-signers file shipped in uun/data, so no new dependency is needed.

CLI (used by .github/workflows/eol-data.yml):
    python3 -m uun.bundle build --upstream full.json [--previous last.json] --out DIR
    python3 -m uun.bundle verify BUNDLE [--sig BUNDLE.sig] [--allowed-signers FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, detect, eol
from .config import EOL_CACHE, data_path
from .eol import _SLUG, normalize_product
from .safety import child_env, clean, read_trusted, which

FORMAT = "uun-eol-bundle"
SCHEMA = 1
NAMESPACE = "uun-eol"
PRINCIPAL = "eol@uun"
UPSTREAM_URL = "https://endoflife.date/api/v1/products/full"
ATTRIBUTION = ("End-of-life data from endoflife.date (https://endoflife.date), "
               "MIT License. Snapshot normalized and signed by uun's eol-data workflow.")

MAX_UPSTREAM_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MIN_PRODUCTS = 300          # gate: upstream tracked 489 products on 2026-11-12
MIN_SURVIVAL = 0.95         # gate: share of upstream products that must normalize
MAX_DISAPPEARED = 0.05      # gate: share of last month's products that may vanish


class BundleError(Exception):
    """A bundle (or its upstream input) failed a check. The message says which."""


def tracked_slugs() -> set[str]:
    """Products uun itself looks up: products.toml + OS releases + kernel."""
    specs = detect.load_specs(data_path("products.toml"))
    return {s.slug for s in specs} | set(detect.OS_MAP.values()) | {"centos-stream", "linux"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_bundle(upstream: dict, previous: dict | None = None, now: str | None = None,
                 tracked: set[str] | None = None) -> tuple[dict, list[str]]:
    """Normalize /products/full and apply the quality gates.

    Returns (bundle, warnings). Raises BundleError when a gate fails: the
    pipeline must stop before signing rather than publish doubtful data.
    """
    warnings: list[str] = []
    if not isinstance(upstream, dict):
        raise BundleError("upstream: not a JSON object")
    schema = upstream.get("schema_version")
    if not isinstance(schema, str) or not schema.startswith("1."):
        raise BundleError(f"upstream schema_version {schema!r} is not 1.x; update the normalizer first")
    items = upstream.get("result")
    if not isinstance(items, list):
        raise BundleError("upstream: 'result' is not a list")
    total = upstream.get("total")
    total = total if isinstance(total, int) and total > 0 else len(items)

    products: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = item.get("name")
        if not isinstance(slug, str) or not _SLUG.match(slug):
            continue
        prod = normalize_product(item, slug)
        if prod is None or not prod["releases"]:
            continue
        products[slug] = prod

    # --- gates -------------------------------------------------------------
    if len(products) < MIN_PRODUCTS:
        raise BundleError(f"only {len(products)} usable products (minimum {MIN_PRODUCTS})")
    if len(products) < MIN_SURVIVAL * total:
        raise BundleError(f"only {len(products)} of {total} upstream products normalized "
                          f"(minimum {MIN_SURVIVAL:.0%})")
    tracked = tracked if tracked is not None else tracked_slugs()
    missing_tracked = sorted(tracked - set(products))
    if previous:
        before = set(previous.get("products", {}))
        gone = sorted(before - set(products))
        if before and len(gone) > MAX_DISAPPEARED * len(before):
            raise BundleError(f"{len(gone)} of {len(before)} products disappeared since "
                              f"{previous.get('month')} (maximum {MAX_DISAPPEARED:.0%}): "
                              f"{', '.join(gone[:10])}{'…' if len(gone) > 10 else ''}")
        regressed = sorted(set(missing_tracked) & before)
        if regressed:
            raise BundleError(f"products uun tracks vanished since {previous.get('month')}: "
                              f"{', '.join(regressed)}")
    if missing_tracked:
        warnings.append(f"tracked by uun but not on endoflife.date: {', '.join(missing_tracked)}")

    generated_at = now or _iso_now()
    upstream_at = upstream.get("generated_at")
    bundle = {
        "format": FORMAT,
        "schema": SCHEMA,
        "month": generated_at[:7],
        "generated_at": generated_at,
        "source": {
            "url": UPSTREAM_URL,
            "schema_version": clean(schema, 16),
            "generated_at": clean(upstream_at, 40) if isinstance(upstream_at, str) else None,
            "total": total,
            "products": len(products),
            "attribution": ATTRIBUTION,
        },
        "products": products,
    }
    if previous and isinstance(previous.get("generated_at"), str) \
            and generated_at <= previous["generated_at"]:
        raise BundleError(f"generated_at {generated_at} is not newer than the previous "
                          f"bundle's {previous['generated_at']}")
    return bundle, warnings


def canonical_bytes(bundle: dict) -> bytes:
    """Sorted keys, no whitespace, ASCII only: same data, same bytes, same signature."""
    return (json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\n").encode("ascii")


def load_bundle(data: bytes) -> dict:
    """Parse and re-validate a bundle (never trust it just because it's signed:
    the same allow-list runs again). Raises BundleError."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError(f"bundle larger than {MAX_BUNDLE_BYTES} bytes")
    try:
        raw = json.loads(data)
    except ValueError as e:
        raise BundleError(f"bundle is not valid JSON: {e}") from None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise BundleError("not a uun EoL bundle")
    if raw.get("schema") != SCHEMA:
        raise BundleError(f"unsupported bundle schema {raw.get('schema')!r} (this uun reads {SCHEMA})")
    gen = raw.get("generated_at")
    if not isinstance(gen, str) or len(gen) != 20 or not gen.endswith("Z"):
        raise BundleError("bundle has no valid generated_at")
    products = {}
    for slug, prod in (raw.get("products") or {}).items():
        if isinstance(slug, str) and _SLUG.match(slug):
            norm = normalize_product(prod, slug)
            if norm is not None:
                products[slug] = norm
    if not products:
        raise BundleError("bundle contains no usable products")
    return {"format": FORMAT, "schema": SCHEMA, "month": clean(raw.get("month", gen[:7]), 7),
            "generated_at": gen, "source": raw.get("source") if isinstance(raw.get("source"), dict) else {},
            "products": products}


def default_allowed_signers() -> Path:
    return data_path("eol-allowed-signers")


def verify_signature(data: bytes, signature: Path, allowed_signers: Path | None = None) -> None:
    """Check an SSH signature (namespace uun-eol, principal eol@uun) with
    ssh-keygen from a trusted directory. Raises BundleError on any failure."""
    # ssh-keygen runs with cwd=/ (safety.SAFE_CWD), so relative paths would
    # resolve against / instead of the caller's directory: make them absolute.
    allowed = Path(allowed_signers or default_allowed_signers()).resolve()
    signature = Path(signature).resolve()
    # same ownership/permission checks as config files: this file decides
    # which key uun trusts
    keys = [ln for ln in read_trusted(allowed).splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    if not keys:
        raise BundleError(f"no signing key configured in {allowed}")
    exe = which("ssh-keygen")
    if not exe:
        raise BundleError("ssh-keygen (OpenSSH) not found in a trusted PATH directory")
    try:
        r = subprocess.run([exe, "-Y", "verify", "-f", str(allowed), "-I", PRINCIPAL,
                            "-n", NAMESPACE, "-s", str(signature)],
                           input=data, capture_output=True, timeout=30, env=child_env(), cwd="/")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BundleError(f"ssh-keygen failed to run: {e}") from None
    if r.returncode != 0:
        msg = clean((r.stderr or r.stdout).decode("utf-8", "replace").strip(), 200)
        raise BundleError(f"signature check failed: {msg or 'exit ' + str(r.returncode)}")


# ------------------------------------------------------------ import (client)

BASELINE = "eol-baseline.json"
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_TAG = re.compile(r"^eol-(\d{4}-\d{2})$")
DOWNLOAD_DEADLINE = 60.0


def iso_epoch(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _describe(data: dict) -> str:
    if data.get("source") == "bundle":
        return f"bundle {data.get('bundle', {}).get('month') or '?'}"
    ts = data.get("as_of")
    when = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "unknown date"
    return f"live endoflife.date data from {when}"


def import_bundle(path: Path, sig: Path | None = None, allowed_signers: Path | None = None,
                  allow_older: bool = False, cache: Path = EOL_CACHE) -> tuple[str, dict]:
    """Verify a signed bundle and make it uun's EoL data.

    Returns ("imported" | "current", bundle). Raises BundleError when anything
    is wrong; the existing cache is left untouched in that case.
    Rollback protection: a bundle must be newer than the data uun already has
    (a live fetch counts by its fetch time), unless allow_older is set.
    """
    path = Path(path)
    try:
        st = path.stat()
        if st.st_size > MAX_BUNDLE_BYTES:
            raise BundleError(f"{path} is larger than {MAX_BUNDLE_BYTES} bytes")
        data = path.read_bytes()
    except OSError as e:
        raise BundleError(f"cannot read {path}: {e.strerror or e}") from None
    sig = Path(sig) if sig else Path(f"{path}.sig")
    if not sig.exists():
        raise BundleError(f"signature {sig} not found (download it next to the bundle)")
    verify_signature(data, sig, allowed_signers)
    b = load_bundle(data)
    new_as_of = iso_epoch(b["generated_at"])
    current = eol.load_cache(cache)
    cur_as_of = current.get("as_of") if current.get("products") else None
    if cur_as_of is not None and not allow_older:
        same = current.get("source") == "bundle" and \
            current.get("bundle", {}).get("generated_at") == b["generated_at"]
        if same:
            return "current", b
        if new_as_of <= cur_as_of:
            raise BundleError(f"refusing to import bundle {b['month']} (made {b['generated_at']}): "
                              f"uun already has newer data ({_describe(current)}). "
                              "Use --allow-older to downgrade on purpose.")
    out = {"fetched_at": time.time(), "as_of": new_as_of, "source": "bundle",
           "bundle": {"month": b["month"], "generated_at": b["generated_at"]},
           "products": b["products"], "errors": {}}
    err = eol.write_cache(out, cache)
    if err:
        raise BundleError(err)
    return "imported", b


def ensure_baseline(cache: Path = EOL_CACHE, baseline: Path | None = None) -> str | None:
    """Fresh offline installs: if the bundle shipped inside the package is newer
    than the cache (or there is no cache), import it. Returns a log message when
    something happened or failed, None when nothing needed doing."""
    baseline = baseline or data_path(BASELINE)
    if not baseline.exists():
        return None
    try:
        raw = json.loads(baseline.read_bytes()[:MAX_BUNDLE_BYTES])
        gen = raw.get("generated_at") if isinstance(raw, dict) else None
        cur = eol.load_cache(cache)
        if cur.get("products") and isinstance(gen, str) and cur.get("as_of") is not None \
                and iso_epoch(gen) <= cur["as_of"]:
            return None
        status, b = import_bundle(baseline, cache=cache)
        return f"EoL data: using the bundled snapshot {b['month']} ({len(b['products'])} products)" \
            if status == "imported" else None
    except (BundleError, ValueError, OSError) as e:
        return f"EoL data: bundled snapshot not used: {e}"


def _download(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"uun/{__version__}",
                                               "Accept": "application/octet-stream, application/json"})
    start, chunks, total = time.monotonic(), [], 0
    try:
        with eol._OPENER.open(req, timeout=15) as r:      # https-only redirects
            while chunk := r.read(65536):
                total += len(chunk)
                if total > limit:
                    raise BundleError(f"{url}: response larger than {limit} bytes")
                if time.monotonic() - start > DOWNLOAD_DEADLINE:
                    raise BundleError(f"{url}: download too slow")
                chunks.append(chunk)
    except urllib.error.HTTPError as e:
        raise BundleError(clean(f"{url}: HTTP {e.code} {e.msg}", 160)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BundleError(clean(f"{url}: {getattr(e, 'reason', e)}", 160)) from None
    return b"".join(chunks)


def latest_release(repo: str) -> tuple[str, str]:
    """Newest eol-YYYY-MM release of `repo` → (tag, month)."""
    if not _REPO.match(repo):
        raise BundleError(f"invalid eol_bundle_repo {repo!r} (expected owner/name)")
    raw = _download(f"https://api.github.com/repos/{repo}/releases?per_page=50", 4 * 1024 * 1024)
    try:
        releases = json.loads(raw)
    except ValueError:
        raise BundleError("GitHub API returned invalid JSON") from None
    months = sorted((m.group(1), r["tag_name"]) for r in releases if isinstance(r, dict)
                    and not r.get("draft") and isinstance(r.get("tag_name"), str)
                    and (m := _TAG.match(r["tag_name"])))
    if not months:
        raise BundleError(f"no eol-YYYY-MM releases found in {repo}")
    month, tag = months[-1]
    return tag, month


def refresh_from_github(repo: str, cache: Path = EOL_CACHE) -> str:
    """eol_source = "bundle": fetch the newest signed bundle and import it.
    Returns a log message; raises BundleError on failure (cache untouched)."""
    tag, month = latest_release(repo)
    base = f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag)}"
    name = f"uun-eol-{month}.json"
    tmp = Path(tempfile.mkdtemp(prefix="uun-eol-"))   # 0700, private to this user
    try:
        (tmp / name).write_bytes(_download(f"{base}/{name}", MAX_BUNDLE_BYTES))
        (tmp / f"{name}.sig").write_bytes(_download(f"{base}/{name}.sig", 64 * 1024))
        status, b = import_bundle(tmp / name, cache=cache)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if status == "current":
        cur = eol.load_cache(cache)
        cur["fetched_at"] = time.time()    # remember the check, so we don't re-ask every start
        eol.write_cache(cur, cache)
        return f"EoL data: already up to date (bundle {b['month']})"
    return f"EoL data: imported signed bundle {b['month']} from {repo} ({len(b['products'])} products)"


# --------------------------------------------------------------------- CLI

def _read_json(path: Path, limit: int) -> dict:
    data = path.read_bytes()
    if len(data) > limit:
        raise BundleError(f"{path} is larger than {limit} bytes")
    try:
        return json.loads(data)
    except ValueError as e:
        raise BundleError(f"{path}: invalid JSON: {e}") from None


def _cmd_build(a) -> int:
    upstream = _read_json(Path(a.upstream), MAX_UPSTREAM_BYTES)
    previous = load_bundle(Path(a.previous).read_bytes()) if a.previous else None
    bundle, warnings = build_bundle(upstream, previous)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    name = f"uun-eol-{bundle['month']}.json"
    (out / name).write_bytes(canonical_bytes(bundle))
    summary = {"file": name, "month": bundle["month"], "products": len(bundle["products"]),
               "upstream_total": bundle["source"]["total"],
               "upstream_generated_at": bundle["source"]["generated_at"],
               "previous": previous["month"] if previous else None, "warnings": warnings}
    print(json.dumps(summary))
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(f"### EoL bundle {bundle['month']}\n\n"
                    f"- products: **{summary['products']}** of {summary['upstream_total']} upstream\n"
                    f"- upstream generated: {summary['upstream_generated_at']}\n"
                    f"- compared with: {summary['previous'] or 'nothing (first bundle)'}\n")
            for w in warnings:
                f.write(f"- warning: {w}\n")
    return 0


def _cmd_verify(a) -> int:
    path = Path(a.bundle)
    data = path.read_bytes()
    verify_signature(data, Path(a.sig or f"{path}.sig"),
                     Path(a.allowed_signers) if a.allowed_signers else None)
    b = load_bundle(data)
    print(f"OK: {path.name}: {len(b['products'])} products, generated {b['generated_at']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m uun.bundle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="normalize /products/full into a bundle (with quality gates)")
    b.add_argument("--upstream", required=True)
    b.add_argument("--previous")
    b.add_argument("--out", required=True)
    b.add_argument("--summary", help="append a Markdown summary (e.g. $GITHUB_STEP_SUMMARY)")
    v = sub.add_parser("verify", help="check a bundle's signature and contents")
    v.add_argument("bundle")
    v.add_argument("--sig")
    v.add_argument("--allowed-signers")
    a = ap.parse_args(argv)
    try:
        return _cmd_build(a) if a.cmd == "build" else _cmd_verify(a)
    except BundleError as e:
        print(f"uun.bundle: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
