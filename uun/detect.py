"""Detect installed software versions for EoL checks."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
    data = tomllib.loads(path.read_text())
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
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "PATH", "TERM")}
    env["PATH"] = os.pathsep.join([env.get("PATH", "/usr/bin:/bin"), *EXTRA_PATH])
    env["LC_ALL"] = "C"
    return env


def _run_probe(argv: list[str], env: dict) -> str | None:
    exe = shutil.which(argv[0], path=env["PATH"])
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=8,
                           env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (r.stdout or "") + "\n" + (r.stderr or "")


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
        found.append(Detected(slug, osr.get("NAME", slug), ver, "/etc/os-release"))
    if platform.system() == "Linux":
        found.append(Detected("linux", "Linux kernel", platform.release(), "uname -r"))
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
