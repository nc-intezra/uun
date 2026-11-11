"""Detect installed software versions for EoL checks."""
from __future__ import annotations

import os
import platform
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .safety import child_env, clean, read_trusted, run_tracked, which

EXTRA_PATH = ["/usr/sbin", "/sbin", "/usr/local/sbin", "/usr/local/bin", "/snap/bin"]

OS_MAP = {  # /etc/os-release ID -> endoflife.date slug
    "ubuntu": "ubuntu", "debian": "debian", "rhel": "rhel", "rocky": "rocky-linux",
    "almalinux": "almalinux", "centos": "centos", "fedora": "fedora",
    "opensuse-leap": "opensuse", "sles": "sles", "amzn": "amazon-linux",
    "alpine": "alpine-linux", "ol": "oracle-linux", "linuxmint": "linuxmint",
}


@dataclass
class ProductSpec:
    slug: str
    label: str
    commands: list[list[str]] = field(default_factory=list)
    regex: str = r"(\d+\.\d+(?:\.\d+)?)"
    exclude: str | None = None
    packages: list[str] = field(default_factory=list)  # fnmatch globs for repo packages


@dataclass
class Detected:
    slug: str
    label: str
    version: str
    source: str
    packages: list[str] = field(default_factory=list)


def load_specs(path: Path) -> list[ProductSpec]:
    data = tomllib.loads(read_trusted(path))
    specs = []
    for p in data.get("product", []):
        cmds = p.get("commands", [])
        if not all(isinstance(c, list) and all(isinstance(x, str) for x in c) for c in cmds):
            raise ValueError(f"{path}: product {p.get('slug')}: commands must be argv lists")
        specs.append(ProductSpec(slug=p["slug"], label=p.get("label", p["slug"]), commands=cmds,
                                 regex=p.get("regex", ProductSpec.regex), exclude=p.get("exclude"),
                                 packages=p.get("packages", [])))
    return specs


def _env() -> dict:
    full = child_env(extra_dirs=tuple(EXTRA_PATH))
    env = {k: v for k, v in full.items() if k in ("HOME", "USER", "PATH", "TERM", "LC_ALL")}
    return env


def _run_probe(argv: list[str], env: dict) -> str | None:
    exe = which(argv[0], env["PATH"].split(os.pathsep))
    if not exe:
        return None
    try:
        # run_tracked uses cwd=/ : `python3 -c "import django"` must not import
        # ./django.py from whatever directory the user launched uun in.
        rc, out, err = run_tracked([exe, *argv[1:]], timeout=8, env=env)
    except OSError:
        return None
    if rc in (124, 130):
        return None
    return (out or "") + "\n" + (err or "")


def os_release() -> dict:
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            out = {}
            for line in Path(p).read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k] = v.strip().strip('"')
            return out
        except OSError:
            continue
    return {}


def detect_os() -> list[Detected]:
    found = []
    osr = os_release()
    oid, ver = osr.get("ID", ""), osr.get("VERSION_ID", "")
    slug = OS_MAP.get(oid)
    if oid == "centos" and "stream" in osr.get("NAME", "").lower():
        slug = "centos-stream"
    if slug and ver:
        found.append(Detected(slug, clean(osr.get("NAME", slug), 40), clean(ver, 20), "/etc/os-release"))
    if platform.system() == "Linux":
        found.append(Detected("linux", "Linux kernel", clean(platform.release(), 40), "uname -r"))
    return found


def detect(specs: list[ProductSpec]) -> list[Detected]:
    env = _env()
    found = detect_os()
    for spec in specs:
        for argv in spec.commands:
            out = _run_probe(argv, env)
            if out is None:
                continue
            if spec.exclude and re.search(spec.exclude, out):
                continue
            m = re.search(spec.regex, out)
            if m:
                found.append(Detected(spec.slug, spec.label, m.group(1), " ".join(argv),
                                      spec.packages))
                break
    return found
