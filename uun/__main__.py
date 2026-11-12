"""Command-line entry point."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="uun", description="Universal update notifier (TUI)")
    ap.add_argument("--theme", help="theme name or path to a .conf file")
    ap.add_argument("--config", help="alternate config.toml")
    ap.add_argument("--refresh-eol", action="store_true", help="fetch EoL data into the cache and exit")
    ap.add_argument("--import-eol", metavar="FILE",
                    help="verify and import a signed EoL bundle (uun-eol-YYYY-MM.json) and exit")
    ap.add_argument("--sig", metavar="FILE", help="with --import-eol: signature file (default FILE.sig)")
    ap.add_argument("--allow-older", action="store_true",
                    help="with --import-eol: accept a bundle older than the current data")
    ap.add_argument("--report", action="store_true", help="print a plain-text report and exit (cron/CI)")
    ap.add_argument("--json", action="store_true", help="with --report: JSON output")
    ap.add_argument("--list-themes", action="store_true")
    ap.add_argument("--demo", action="store_true", help="run the TUI on canned data (safe preview)")
    ap.add_argument("--version", action="version", version=f"uun {__version__}")
    a = ap.parse_args(argv)

    from pathlib import Path
    from .config import ConfigError, UnsafeFileError, load_config
    try:
        cfg = load_config(Path(a.config) if a.config else None)
    except UnsafeFileError as e:
        print(f"uun: refusing to run: {e}", file=sys.stderr)
        return 3
    except ConfigError as e:
        print(f"uun: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # e.g. invalid TOML: one clear line, no traceback
        print(f"uun: cannot load configuration: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    if a.theme:
        cfg.theme = a.theme

    if a.list_themes:
        from .theme import list_themes
        print("\n".join(list_themes()))
        return 0
    if a.import_eol:
        from . import bundle
        try:
            status, b = bundle.import_bundle(Path(a.import_eol), Path(a.sig) if a.sig else None,
                                             allow_older=a.allow_older)
        except bundle.BundleError as e:
            print(f"uun: {e}", file=sys.stderr)
            return 1
        if status == "current":
            print(f"uun: EoL bundle {b['month']} is already in use")
        else:
            print(f"uun: imported EoL bundle {b['month']} "
                  f"({len(b['products'])} products, generated {b['generated_at']})")
        return 0
    if a.refresh_eol:
        from . import scan
        res = scan.refresh_eol(cfg)
        for m in res["messages"]:
            print(f"uun: {m}")
        for w in res["warnings"]:
            print(f"uun: {w}", file=sys.stderr)
        for slug, err in res.get("errors", {}).items():
            print(f"  {slug}: {err}", file=sys.stderr)
        return 0 if res["ok"] else 1
    if a.report:
        from . import scan
        return scan.report(cfg, as_json=a.json)

    if not sys.stdout.isatty():
        print("uun: stdout is not a terminal; use --report", file=sys.stderr)
        return 1
    from .app import UunApp
    from .safety import stop_background
    app = UunApp(cfg, demo=a.demo)
    app.run()
    # The terminal is restored at this point. Stop background checks (process
    # groups) and exit right away: otherwise Python waits at shutdown for
    # check threads still blocked on slow tools/network (up to their timeouts).
    # Nothing here holds state that needs flushing: the EoL cache is written
    # atomically, and package updates can't be running (quit is refused then).
    stop_background()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(app.return_code or 0)


if __name__ == "__main__":
    sys.exit(main())
