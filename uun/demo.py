"""Canned data for `uun --demo`: try themes/keys without touching the system."""
from __future__ import annotations

import time

from .detect import Detected
from .managers import CheckResult, Manager


def _noop():
    return CheckResult()


def managers() -> list[Manager]:
    return [Manager(n, n, f, _noop, s) for n, f, s in (
        ("apt", "apt", True), ("snap", "snap", False), ("flatpak", "flatpak", False),
        ("pip", "pip", False), ("pipx", "pipx", False), ("cargo", "cargo", False),
        ("rustup", "rustup", False), ("npm", "npm", False))]


def checks() -> dict[str, CheckResult]:
    return {
        "apt": CheckResult(14, 5, {"openssl": "3.0.2-0ubuntu1.20", "libssl3": "3.0.2-0ubuntu1.20",
                                   "nginx-core": "1.18.0-6ubuntu14.7", "python3": "3.10.12-1~22.04.11"},
                           note="index 3h old"),
        "snap": CheckResult(0, None, {}),
        "flatpak": CheckResult(2, None, {"org.mozilla.firefox": "144.0"}),
        "pip": CheckResult(3, None, {"requests": "2.33.0", "rich": "14.2.0", "urllib3": "2.6.1"},
                           note="user site"),
        "pipx": CheckResult(None, note="4 apps (no pre-check)"),
        "cargo": CheckResult(2, None, {"ripgrep": "15.1.0", "bat": "0.26.0"}),
        "rustup": CheckResult(1, None, {"stable-x86_64-unknown-linux-gnu": "1.91.0"}),
        "npm": CheckResult(error="npm ERR! EACCES: permission denied, /usr/lib/node_modules"),
    }


def detected() -> list[Detected]:
    return [
        Detected("ubuntu", "Ubuntu", "22.04", "/etc/os-release"),
        Detected("linux", "Linux kernel", "5.15.0-160-generic", "uname -r"),
        Detected("mysql", "MySQL", "8.0.43", "mysqld --version", ["mysql-server*"]),
        Detected("python", "Python", "3.10.12", "python3 --version", ["python3"]),
        Detected("nodejs", "Node.js", "20.19.5", "node --version", ["nodejs"]),
        Detected("redis", "Redis", "6.0.16", "redis-server --version", ["redis-server"]),
        Detected("nginx", "nginx", "1.18.0", "nginx -v", ["nginx-core"]),
        Detected("openssl", "OpenSSL", "3.0.2", "openssl version", ["openssl"]),
        Detected("postgresql", "PostgreSQL", "17.6", "psql --version", ["postgresql-17"]),
        Detected("docker-engine", "Docker Engine", "28.5.1", "docker --version", ["docker-ce"]),
    ]


def _r(name, rel, eol, latest, lts=False):
    return {"name": name, "label": name, "releaseDate": rel, "isLts": lts,
            "isEol": eol is not None and eol < "2026-11-11", "eolFrom": eol, "latest": latest}


def cache() -> dict:
    return {"fetched_at": time.time() - 7200, "products": {
        "ubuntu": {"label": "Ubuntu", "html": "https://endoflife.date/ubuntu", "releases": [
            _r("26.04", "2026-04-23", "2031-05-31", "26.04", True),
            _r("24.04", "2024-04-25", "2029-05-31", "24.04.3", True),
            _r("22.04", "2022-04-21", "2027-06-01", "22.04.5", True)]},
        "linux": {"label": "Linux Kernel", "html": "https://endoflife.date/linux", "releases": [
            _r("6.12", "2024-11-17", "2026-12-31", "6.12.57", True),
            _r("5.15", "2021-10-31", "2026-12-31", "5.15.196", True)]},
        "mysql": {"label": "MySQL", "html": "https://endoflife.date/mysql", "releases": [
            _r("26.10", "2026-10-20", None, "26.10.0"),
            _r("8.4", "2024-04-30", "2032-04-30", "8.4.7", True),
            _r("8.0", "2018-04-19", "2026-04-30", "8.0.44")]},
        "python": {"label": "Python", "html": "https://endoflife.date/python", "releases": [
            _r("3.14", "2025-10-07", "2030-10-31", "3.14.0"),
            _r("3.13", "2024-10-07", "2029-10-31", "3.13.9"),
            _r("3.10", "2021-10-04", "2026-10-31", "3.10.19")]},
        "nodejs": {"label": "Node.js", "html": "https://endoflife.date/nodejs", "releases": [
            _r("24", "2025-05-06", "2028-04-30", "24.11.0", True),
            _r("22", "2024-04-24", "2027-04-30", "22.21.1", True),
            _r("20", "2023-04-18", "2026-04-30", "20.19.5", True)]},
        "redis": {"label": "Redis", "html": "https://endoflife.date/redis", "releases": [
            _r("8.2", "2025-08-04", None, "8.2.3"),
            _r("6.0", "2020-04-30", "2023-08-31", "6.0.20")]},
        "nginx": {"label": "nginx", "html": "https://endoflife.date/nginx", "releases": [
            _r("1.29", "2025-06-24", None, "1.29.3"),
            _r("1.18", "2020-04-21", "2021-04-20", "1.18.0")]},
        "openssl": {"label": "OpenSSL", "html": "https://endoflife.date/openssl", "releases": [
            _r("3.5", "2025-04-08", "2030-04-08", "3.5.4", True),
            _r("3.0", "2021-09-07", "2026-09-07", "3.0.18", True)]},
        "postgresql": {"label": "PostgreSQL", "html": "https://endoflife.date/postgresql", "releases": [
            _r("18", "2025-09-25", "2030-11-14", "18.1"),
            _r("17", "2024-09-26", "2029-11-08", "17.7")]},
        "docker-engine": {"label": "Docker Engine", "html": "https://endoflife.date/docker-engine", "releases": [
            _r("29.0", "2025-11-10", None, "29.0.0"),
            _r("28.5", "2025-10-02", "2026-12-15", "28.5.2")]},
    }}


LOG = [
    "EoL data: 41 products (cached 2h ago)",
    "apt: 14 pending (5 security) — package index 3h old",
    "npm: global prefix is root-owned; set `prefix` in ~/.npmrc or enable sudo for npm in config.toml",
    "scan done: 22 pending updates, 8 EoL item(s) need attention",
]
