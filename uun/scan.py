"""Glue between detection, package managers and EoL data (shared by TUI and CLI)."""
from __future__ import annotations

import fnmatch
import json
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import detect, eol, managers
from .config import Config
from .safety import clean, read_trusted

STATE_ORDER = {"EOL": 0, "CRIT": 1, "SOON": 2, "UNKNOWN": 3, "OK": 4}


def all_slugs(cfg: Config) -> list[str]:
    specs = detect.load_specs(cfg.products_file)
    return sorted({s.slug for s in specs} | set(detect.OS_MAP.values()) | {"centos-stream", "linux"})


def check_all(mgrs: list[managers.Manager]) -> dict[str, managers.CheckResult]:
    def one(m):
        try:
            return m.name, m.check()
        except Exception as e:  # a broken manager must not kill the scan
            return m.name, managers.CheckResult(error=f"{type(e).__name__}: {e}")
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(one, mgrs))


def _pending_for(d: detect.Detected, checks: dict[str, managers.CheckResult]) -> str | None:
    for res in checks.values():
        for pkg, ver in res.pending.items():
            if any(fnmatch.fnmatch(pkg, g) for g in d.packages):
                return ver
    return None


def evaluate_all(cfg: Config, found: list[detect.Detected], checks: dict,
                 cache: dict | None = None) -> list[eol.EolStatus]:
    cache = cache if cache is not None else eol.load_cache()
    products = cache.get("products", {})
    out, seen = [], set()
    for d in found:
        if d.slug in seen:
            continue
        seen.add(d.slug)
        raw = products.get(d.slug)
        prod = eol.normalize_product(raw, d.slug) if raw is not None else None
        st = eol.evaluate(d.slug, d.version, d.source, prod, cfg.warn_days, cfg.critical_days)
        if st.label == d.slug:
            st.label = d.label
        eol.apply_inbound(st, _pending_for(d, checks), prod, cfg.warn_days)
        out.append(st)
    out.sort(key=lambda s: (STATE_ORDER[s.state], s.days_left if s.days_left is not None else 10**6))
    return out


def eol_refresh_due(cfg: Config, cache: Path = eol.EOL_CACHE) -> bool:
    """Whether the TUI should refresh EoL data on start (auto_refresh_eol)."""
    if cfg.eol_source == "offline" or not cfg.auto_refresh_eol:
        return False
    data = eol.load_cache(cache)
    if not data.get("products"):
        return True
    # live data ages from its fetch; for bundles, from the last time we checked
    # GitHub (a bundle's own as-of date is weeks old by design)
    ts = data.get("fetched_at") if cfg.eol_source == "bundle" else data.get("as_of")
    return ts is None or (time.time() - ts) / 3600 > cfg.eol_max_age_hours


def refresh_eol(cfg: Config, cache: Path = eol.EOL_CACHE) -> dict:
    """Refresh EoL data from the configured source.

    Returns {"ok": bool, "messages": [str], "warnings": [str], "session": dict|None}
    where "session" is live data that could not be saved (use it for this run).
    """
    from . import bundle
    out: dict = {"ok": True, "messages": [], "warnings": [], "session": None}
    note = bundle.ensure_baseline(cache)
    if note:
        (out["warnings"] if "not used" in note else out["messages"]).append(note)
    if cfg.eol_source == "offline":
        info = eol.cache_info(cache)
        out["messages"].append(f"EoL data: offline mode, using {info['label']} "
                               "(import new data with `uun --import-eol FILE`)")
        out["ok"] = info["source"] is not None
        return out
    if cfg.eol_source == "bundle":
        try:
            out["messages"].append(bundle.refresh_from_github(cfg.eol_bundle_repo, cache))
        except bundle.BundleError as e:
            out["ok"] = False
            out["warnings"].append(f"EoL bundle refresh failed: {e}")
        return out
    res = eol.refresh(cfg.eol_api, all_slugs(cfg), cache)
    errs = res.get("errors", {})
    out["messages"].append(f"EoL data: {len(res['products'])} products"
                           + (f", {len(errs)} failed: {', '.join(list(errs)[:5])}" if errs else ""))
    out["errors"] = errs
    if res.get("cache_error"):
        out["session"] = res
        out["warnings"].append(res["cache_error"])
    out["ok"] = bool(res["products"]) and not res.get("cache_error")
    return out


def data_status(cfg: Config, cache: Path = eol.EOL_CACHE, data: dict | None = None) -> tuple[str, bool]:
    """(label, stale) for the titlebar and reports."""
    info = eol.cache_info(cache, data)
    stale = info["source"] is None or (info["age_days"] is not None and info["age_days"] > cfg.stale_days)
    return info["label"], stale


def load_alternatives(path: Path) -> dict:
    return tomllib.loads(read_trusted(path))


def report(cfg: Config, as_json: bool = False) -> int:
    """Non-interactive report for cron/CI. Exit: 0 ok, 1 attention, 2 EoL present."""
    from . import bundle
    note = bundle.ensure_baseline()          # fresh offline install: use the packaged snapshot
    if note:
        print(f"uun: {note}", file=sys.stderr)
    mgrs = managers.available(cfg.managers)
    checks = check_all(mgrs)
    found = detect.detect(detect.load_specs(cfg.products_file))
    statuses = evaluate_all(cfg, found, checks)
    alts = load_alternatives(cfg.alternatives_file)
    info = eol.cache_info()
    data_s, stale = data_status(cfg, data=None)
    if as_json:
        print(json.dumps({
            "eol_data": {"source": info["source"], "label": info["label"],
                         "age_days": None if info["age_days"] is None else round(info["age_days"], 1),
                         "stale": stale},
            "managers": {k: {"count": v.count, "security": v.security, "note": v.note,
                             "error": v.error} for k, v in checks.items()},
            "eol": [{**{k: (str(v) if v is not None else None) for k, v in s.__dict__.items()},
                     "alternatives": [a["name"] for a in alts.get(s.slug, {}).get("alternatives", [])]
                     if s.suggest_alternative else []} for s in statuses],
        }, indent=2))
    else:
        print(f"EoL data: {data_s}" + (f"  (WARNING: older than {cfg.stale_days} days)" if stale else "") + "\n")
        print("== Package managers ==")
        for k, v in checks.items():
            cnt = "?" if v.count is None else v.count
            sec = "" if not v.security else f" ({v.security} security)"
            print(f"  {k:<8} {cnt:>4} pending{sec}  {clean(v.error or v.note)}")
        print("\n== End-of-life ==")
        for s in statuses:
            days = "" if s.days_left is None else f"{s.days_left:>5}d"
            print(f"  [{s.state:<7}] {s.label:<24} {s.installed:<12} cycle {s.cycle or '-':<8}"
                  f" EoL {s.eol or '-'} {days}")
            if s.suggest_alternative:
                names = [a["name"] for a in alts.get(s.slug, {}).get("alternatives", [])]
                if s.upgrade_to:
                    print(f"             upgrade path: {s.upgrade_to} (EoL {s.upgrade_eol or 'n/a'})")
                if names:
                    print(f"             alternatives: {', '.join(names)}")
    if any(s.state == "EOL" for s in statuses):
        return 2
    return 1 if any(s.needs_attention for s in statuses) else 0
