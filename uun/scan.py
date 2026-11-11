"""Glue between detection, package managers and EoL data (shared by TUI and CLI)."""
from __future__ import annotations

import fnmatch
import json
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import detect, eol, managers
from .config import Config

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
        prod = products.get(d.slug)
        st = eol.evaluate(d.slug, d.version, d.source, prod, cfg.warn_days, cfg.critical_days)
        if st.label == d.slug:
            st.label = d.label
        eol.apply_inbound(st, _pending_for(d, checks), prod, cfg.warn_days)
        out.append(st)
    out.sort(key=lambda s: (STATE_ORDER[s.state], s.days_left if s.days_left is not None else 10**6))
    return out


def load_alternatives(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def report(cfg: Config, as_json: bool = False) -> int:
    """Non-interactive report for cron/CI. Exit: 0 ok, 1 attention, 2 EoL present."""
    mgrs = managers.available(cfg.managers)
    checks = check_all(mgrs)
    found = detect.detect(detect.load_specs(cfg.products_file))
    statuses = evaluate_all(cfg, found, checks)
    alts = load_alternatives(cfg.alternatives_file)
    if as_json:
        print(json.dumps({
            "managers": {k: {"count": v.count, "security": v.security, "note": v.note,
                             "error": v.error} for k, v in checks.items()},
            "eol": [{**{k: (str(v) if v is not None else None) for k, v in s.__dict__.items()},
                     "alternatives": [a["name"] for a in alts.get(s.slug, {}).get("alternatives", [])]
                     if s.suggest_alternative else []} for s in statuses],
        }, indent=2))
    else:
        print("== Package managers ==")
        for k, v in checks.items():
            cnt = "?" if v.count is None else v.count
            sec = "" if not v.security else f" ({v.security} security)"
            print(f"  {k:<8} {cnt:>4} pending{sec}  {v.error or v.note}")
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
