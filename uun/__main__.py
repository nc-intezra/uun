"""Command-line entry point."""
from __future__ import annotations

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="uun", description="Universal update notifier (TUI)")
    ap.add_argument("--theme", help="theme name or path to a .conf file")
    ap.add_argument("--config", help="alternate config.toml")
    ap.add_argument("--refresh-eol", action="store_true", help="fetch EoL data into the cache and exit")
    ap.add_argument("--report", action="store_true", help="print a plain-text report and exit (cron/CI)")
    ap.add_argument("--json", action="store_true", help="with --report: JSON output")
    ap.add_argument("--list-themes", action="store_true")
    ap.add_argument("--demo", action="store_true", help="run the TUI on canned data (safe preview)")
    ap.add_argument("--version", action="version", version=f"uun {__version__}")
    a = ap.parse_args(argv)

    from pathlib import Path
    from .config import UnsafeFileError, load_config
    try:
        cfg = load_config(Path(a.config) if a.config else None)
    except UnsafeFileError as e:
        print(f"uun: refusing to run: {e}", file=sys.stderr)
        return 3
    if a.theme:
        cfg.theme = a.theme

    if a.list_themes:
        from .theme import list_themes
        print("\n".join(list_themes()))
        return 0
    if a.refresh_eol:
        from . import eol, scan
        res = eol.refresh(cfg.eol_api, scan.all_slugs(cfg))
        print(f"uun: cached {len(res['products'])} products")
        for slug, err in res.get("errors", {}).items():
            print(f"  {slug}: {err}", file=sys.stderr)
        return 0 if res["products"] else 1
    if a.report:
        from . import scan
        return scan.report(cfg, as_json=a.json)

    if not sys.stdout.isatty():
        print("uun: stdout is not a terminal; use --report", file=sys.stderr)
        return 1
    from .app import UunApp
    UunApp(cfg, demo=a.demo).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
