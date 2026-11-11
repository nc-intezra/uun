"""End-of-life data: fetch from endoflife.date, cache, and evaluate."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import __version__
from .config import EOL_CACHE
from .safety import clean, clean_date, clean_url

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RELNAME = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
MAX_BYTES = 2_000_000
DEADLINE = 45.0          # whole-request budget; per-socket timeouts alone allow slow-drip hangs
MAX_RELEASES = 500


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows https -> http redirects by default; refuse any downgrade."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "refusing non-HTTPS redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirect)


def normalize_product(result, fallback_label: str) -> dict | None:
    """Strict allow-list of fields/types. Everything shown in the terminal later
    passes through here, both fresh from the network and when read from cache."""
    if not isinstance(result, dict) or not isinstance(result.get("releases"), list):
        return None
    releases = []
    for rel in result["releases"][:MAX_RELEASES]:
        if not isinstance(rel, dict) or not isinstance(rel.get("name"), str) \
                or not _RELNAME.match(rel["name"]):
            continue
        latest = rel.get("latest")
        if isinstance(latest, dict):
            latest = latest.get("name")
        releases.append({
            "name": rel["name"],
            "releaseDate": clean_date(rel.get("releaseDate")),
            "eolFrom": clean_date(rel.get("eolFrom")),
            "isLts": rel.get("isLts") is True,
            "isEol": rel.get("isEol") is True,
            "latest": latest if isinstance(latest, str) and _RELNAME.match(latest) else None,
        })
    links = result.get("links")
    html = result.get("html")
    if isinstance(links, dict):
        html = links.get("html")
    return {"label": clean(result.get("label", fallback_label), 40),
            "releases": releases, "html": clean_url(html)}


def _fetch_one(api: str, slug: str, timeout: float) -> tuple[str, dict | None, str | None]:
    if not _SLUG.match(slug):
        return slug, None, "invalid product slug"
    url = f"{api.rstrip('/')}/{urllib.parse.quote(slug)}"
    if not url.startswith("https://"):
        return slug, None, "refusing non-HTTPS API URL"
    req = urllib.request.Request(url, headers={"User-Agent": f"uun/{__version__}",
                                               "Accept": "application/json"})
    try:
        start = time.monotonic()
        chunks, total = [], 0
        with _OPENER.open(req, timeout=timeout) as r:  # TLS verified by default
            while chunk := r.read(65536):
                total += len(chunk)
                if total > MAX_BYTES:
                    return slug, None, "response too large"
                if time.monotonic() - start > DEADLINE:
                    return slug, None, "response too slow"
                chunks.append(chunk)
        data = json.loads(b"".join(chunks))
        prod = normalize_product(data.get("result") if isinstance(data, dict) else None, slug)
        if prod is None:
            return slug, None, "bad data: unexpected schema"
        return slug, prod, None
    except urllib.error.HTTPError as e:
        return slug, None, clean(f"HTTP {e.code} {e.msg}", 80)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return slug, None, clean(getattr(e, "reason", e), 80)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return slug, None, clean(f"bad data: {e}", 80)


def refresh(api: str, slugs: list[str], cache: Path = EOL_CACHE, timeout: float = 15) -> dict:
    """Fetch products and merge into the cache. Products that fail keep old data."""
    old = load_cache(cache)
    products = dict(old.get("products", {}))
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for slug, data, err in pool.map(lambda s: _fetch_one(api, s, timeout), sorted(set(slugs))):
            if data:
                products[slug] = data
            else:
                errors[slug] = err or "unknown error"
    out = {"fetched_at": time.time(), "products": products, "errors": errors}
    cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=".eol-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(out, f)
    os.replace(tmp, cache)
    return out


def load_cache(cache: Path = EOL_CACHE) -> dict:
    """Load and re-validate the cache; a tampered or corrupt cache is ignored
    rather than crashing the TUI or injecting text into the terminal."""
    try:
        fd = os.open(cache, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        st = os.fstat(fd)
        if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022 or st.st_size > 20 * MAX_BYTES:
            return {}
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = json.loads(f.read())
    except (OSError, ValueError):
        return {}
    finally:
        os.close(fd)
    if not isinstance(data, dict) or not isinstance(data.get("products"), dict):
        return {}
    products = {}
    for slug, raw in data["products"].items():
        if isinstance(slug, str) and _SLUG.match(slug):
            prod = normalize_product(raw, slug)
            if prod is not None:
                products[slug] = prod
    ts = data.get("fetched_at")
    errors = data.get("errors") if isinstance(data.get("errors"), dict) else {}
    return {"fetched_at": ts if isinstance(ts, (int, float)) else None, "products": products,
            "errors": {clean(k, 64): clean(v, 80) for k, v in errors.items()}}


def cache_age_hours(cache: Path = EOL_CACHE) -> float | None:
    data = load_cache(cache)
    ts = data.get("fetched_at")
    return (time.time() - ts) / 3600 if isinstance(ts, (int, float)) else None


# ---------------------------------------------------------------- evaluation

_VER = re.compile(r"(\d+(?:\.\d+)*)")


def clean_version(v: str) -> str:
    """'1:10.11.6-0+deb12u1' -> '10.11.6'; 'v20.11.1' -> '20.11.1'"""
    v = v.split(":", 1)[1] if re.match(r"^\d+:", v) else v
    m = _VER.search(v)
    return m.group(1) if m else clean(v, 40)


def match_release(version: str, releases: list[dict]) -> dict | None:
    v = clean_version(version)
    best = None
    for rel in releases:
        n = rel["name"]
        if v == n or v.startswith(n + "."):
            if best is None or len(n) > len(best["name"]):
                best = rel
    return best


def _d(s) -> date | None:
    try:
        return date.fromisoformat(s) if isinstance(s, str) else None
    except ValueError:
        return None


@dataclass
class EolStatus:
    slug: str
    label: str
    installed: str
    source: str
    cycle: str | None = None
    eol: date | None = None
    days_left: int | None = None
    state: str = "UNKNOWN"         # OK / SOON / CRIT / EOL / UNKNOWN
    latest_in_cycle: str | None = None
    upgrade_to: str | None = None
    upgrade_eol: date | None = None
    inbound: str | None = None     # pending repo version, if any
    inbound_fixes: bool = False    # pending version lands on a supported cycle
    url: str | None = None
    note: str = ""

    @property
    def needs_attention(self) -> bool:
        return self.state in ("EOL", "CRIT", "SOON")

    @property
    def suggest_alternative(self) -> bool:
        return self.needs_attention and not self.inbound_fixes


def evaluate(slug: str, installed: str, source: str, product: dict | None,
             warn_days: int, critical_days: int, today: date | None = None) -> EolStatus:
    today = today or date.today()
    installed, source = clean(installed, 40), clean(source, 80)
    st = EolStatus(slug=slug, label=clean((product or {}).get("label", slug), 40),
                   installed=installed, source=source)
    if not product:
        st.note = "no EoL data cached (run: uun --refresh-eol)"
        return st
    st.url = product.get("html")
    releases = product["releases"]
    rel = match_release(installed, releases)
    if rel is None:
        st.note = "installed version not found in EoL data"
        return st
    st.cycle = rel["name"]
    st.latest_in_cycle = rel.get("latest")
    st.eol = _d(rel.get("eolFrom"))
    if st.eol:
        st.days_left = (st.eol - today).days
        if st.days_left < 0:
            st.state = "EOL"
        elif st.days_left <= critical_days:
            st.state = "CRIT"
        elif st.days_left <= warn_days:
            st.state = "SOON"
        else:
            st.state = "OK"
    elif rel.get("isEol") is True:
        st.state = "EOL"
    else:
        st.state = "OK"   # no announced EoL date
    # best upgrade target: newest maintained release newer than ours (prefer LTS)
    ours = _d(rel.get("releaseDate")) or date.min
    newer = [r for r in releases
             if r is not rel and (_d(r.get("releaseDate")) or date.min) > ours
             and not r.get("isEol") and ((_d(r.get("eolFrom")) or date.max) > today)]
    if newer:
        lts = [r for r in newer if r.get("isLts")]
        pick = max(lts or newer, key=lambda r: _d(r.get("releaseDate")) or date.min)
        st.upgrade_to = pick.get("latest") or pick["name"]
        st.upgrade_eol = _d(pick.get("eolFrom"))
    return st


def apply_inbound(st: EolStatus, pending_version: str | None, product: dict | None,
                  warn_days: int, today: date | None = None) -> None:
    """Record a pending package-manager update and whether it resolves the EoL."""
    if not pending_version:
        return
    today = today or date.today()
    st.inbound = clean_version(pending_version)
    if not product:
        return
    rel = match_release(st.inbound, product["releases"])
    if rel and rel["name"] != st.cycle:
        eol = _d(rel.get("eolFrom"))
        st.inbound_fixes = not rel.get("isEol") and (eol is None or (eol - today).days > warn_days)
