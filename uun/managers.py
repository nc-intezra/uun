"""Package-manager adapters: detect, check for pending updates, build update commands."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .safety import SAFE_CWD, child_env, clean, sudo_path, which


@dataclass
class CheckResult:
    count: int | None = None              # None = unknown
    security: int | None = None
    pending: dict[str, str] = field(default_factory=dict)   # package -> new version
    note: str = ""
    error: str = ""


def run(argv: list[str], timeout: int = 300, ok=(0,)) -> tuple[int, str, str]:
    # Executables come only from trusted PATH dirs (no '.', relative or
    # world-writable entries) and run with cwd=/ so per-directory config such
    # as ./.npmrc, ./.cargo/config.toml or ./pip/ in the launch dir is ignored.
    exe = which(argv[0])
    if not exe:
        return 127, "", f"{argv[0]}: not found in trusted PATH"
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                           env=child_env(), stdin=subprocess.DEVNULL, cwd=SAFE_CWD,
                           errors="replace")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as e:
        return 126, "", str(e)


def _first(text: str, limit: int = 160) -> str:
    """First non-empty line of a tool's error output, trimmed."""
    for line in text.splitlines():
        if line.strip():
            return clean(line.strip(), limit)
    return ""


def _age(path: str) -> str:
    try:
        h = (time.time() - Path(path).stat().st_mtime) / 3600
    except OSError:
        return ""
    return f"index {h:.0f}h old" if h < 48 else f"index {h / 24:.0f}d old"


# ------------------------------------------------------------------ checkers

def check_apt() -> CheckResult:
    rc, out, err = run(["apt", "list", "--upgradable"])
    res = CheckResult(note=_age("/var/cache/apt/pkgcache.bin"))
    if rc != 0:
        res.error = _first(err) or f"exit {rc}"
        return res
    sec = 0
    for line in out.splitlines():
        m = re.match(r"^([^/\s]+)/(\S+)\s+(\S+)\s", line)
        if m:
            res.pending[m.group(1)] = m.group(3)
            sec += "-security" in m.group(2)
    res.count, res.security = len(res.pending), sec
    return res


def _check_rpm(tool: str) -> CheckResult:
    rc, out, err = run([tool, "-q", "check-update"])
    res = CheckResult()
    if rc not in (0, 100):
        res.error = (err or out).strip().splitlines()[-1:][0][:200] if (err or out).strip() else f"exit {rc}"
        return res
    for line in out.splitlines():
        if line.startswith(("Obsoleting", "Security:", "Last metadata", " ")):
            if line.startswith("Obsoleting"):
                break
            continue
        parts = line.split()
        if len(parts) >= 3 and "." in parts[0]:
            res.pending[parts[0].rsplit(".", 1)[0]] = parts[1]
    res.count = len(res.pending)
    rc, out, _ = run([tool, "-q", "updateinfo", "list", "--security"], timeout=120)
    if rc == 0:
        res.security = sum(1 for ln in out.splitlines()
                           if re.search(r"/Sec\.|\bsecurity\b", ln, re.I) and not ln.lower().startswith("name"))
    return res


def check_dnf() -> CheckResult:
    return _check_rpm("dnf")


def check_yum() -> CheckResult:
    return _check_rpm("yum")


def check_zypper() -> CheckResult:
    rc, out, err = run(["zypper", "-q", "-n", "--no-refresh", "list-updates"])
    res = CheckResult()
    if rc != 0:
        res.error = _first(err) or f"exit {rc}"
        return res
    for line in out.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) >= 5 and cols[0] == "v":
            res.pending[cols[2]] = cols[4]
    res.count = len(res.pending)
    rc, out, _ = run(["zypper", "-q", "-n", "--no-refresh", "list-patches", "--category", "security"])
    if rc in (0, 100, 101):
        res.security = sum(1 for ln in out.splitlines() if ln.count("|") >= 4
                           and "security" in ln.lower() and not ln.lower().startswith("repository"))
    return res


def check_pacman() -> CheckResult:
    res = CheckResult()
    if which("checkupdates"):
        rc, out, err = run(["checkupdates"])
        if rc == 2:
            out = ""
        elif rc != 0:
            res.error = _first(err) or f"exit {rc}"
            return res
    else:
        rc, out, _ = run(["pacman", "-Qu"])
        res.note = "install pacman-contrib for fresh checks"
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 4 and p[2] == "->":
            res.pending[p[0]] = p[3]
    res.count = len(res.pending)
    if which("arch-audit"):
        rc, out, _ = run(["arch-audit", "-u"])
        res.security = len([ln for ln in out.splitlines() if ln.strip()])
    return res


def check_flatpak() -> CheckResult:
    rc, out, err = run(["flatpak", "remote-ls", "--updates", "--columns=application,version"])
    res = CheckResult()
    if rc != 0:
        res.error = _first(err) or f"exit {rc}"
        return res
    for line in out.splitlines():
        p = line.split("\t")
        if p and p[0].strip():
            res.pending[p[0].strip()] = p[1].strip() if len(p) > 1 else ""
    res.count = len(res.pending)
    return res


def check_snap() -> CheckResult:
    rc, out, err = run(["snap", "refresh", "--list"])
    res = CheckResult()
    if rc != 0 and "up to date" not in (out + err):
        res.error = _first(err) or f"exit {rc}"
        return res
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) >= 2:
            res.pending[p[0]] = p[1]
    res.count = len(res.pending)
    return res


def check_brew() -> CheckResult:
    rc, out, err = run(["brew", "outdated", "--json=v2"])
    res = CheckResult()
    if rc != 0:
        res.error = _first(err) or f"exit {rc}"
        return res
    data = json.loads(out or "{}")
    for f in data.get("formulae", []) + data.get("casks", []):
        res.pending[f["name"]] = str(f.get("current_version", ""))
    res.count = len(res.pending)
    return res


_PKG_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,200}[A-Za-z0-9])?$")


def _pip_base() -> list[str]:
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        py = os.path.join(venv, "bin", "python")
        if os.path.isabs(py) and os.access(py, os.X_OK):
            return [py, "-m", "pip"]
    return ["python3", "-m", "pip"]


def _pip_scope() -> list[str]:
    return [] if os.environ.get("VIRTUAL_ENV") else ["--user"]


def check_pip() -> CheckResult:
    rc, out, err = run([*_pip_base(), "list", "--outdated", "--format=json",
                        "--disable-pip-version-check", *_pip_scope()], timeout=180)
    res = CheckResult(note="venv: " + os.environ["VIRTUAL_ENV"] if os.environ.get("VIRTUAL_ENV") else "user site")
    if rc != 0:
        res.error = err.strip().splitlines()[-1][:200] if err.strip() else f"exit {rc}"
        return res
    for p in json.loads(out or "[]"):
        name = str(p.get("name", ""))
        if _PKG_NAME.match(name):          # never let a dist name become a pip option
            res.pending[name] = str(p.get("latest_version", ""))
    res.count = len(res.pending)
    return res


def check_pipx() -> CheckResult:
    rc, out, _ = run(["pipx", "list", "--short"])
    n = len([ln for ln in out.splitlines() if ln.strip()])
    return CheckResult(count=None, note=f"{n} apps (no pre-check)")


def check_cargo() -> CheckResult:
    res = CheckResult()
    if not which("cargo-install-update"):
        res.note = "needs: cargo install cargo-update"
        return res
    rc, out, err = run(["cargo", "install-update", "--list"], timeout=180)
    if rc != 0:
        res.error = _first(err) or f"exit {rc}"
        return res
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 4 and p[-1] == "Yes":
            res.pending[p[0]] = p[2].lstrip("v")
    res.count = len(res.pending)
    return res


def check_rustup() -> CheckResult:
    rc, out, err = run(["rustup", "check"], timeout=120)
    res = CheckResult()
    for line in out.splitlines():
        m = re.match(r"^(\S+) - Update available : \S+ .*?-> (\S+)", line)
        if m:
            res.pending[m.group(1)] = m.group(2)
    res.count = len(res.pending)
    if rc not in (0, 100) and not res.pending and err.strip():
        res.count, res.error = None, _first(err)
    return res


def check_npm() -> CheckResult:
    rc, out, err = run(["npm", "outdated", "-g", "--json"], timeout=180)
    res = CheckResult()
    try:
        data = json.loads(out or "{}")
    except ValueError:
        res.error = _first(err) or f"exit {rc}" or "unparseable npm output"
        return res
    for name, info in data.items():
        res.pending[name] = info.get("latest", "")
    res.count = len(res.pending)
    return res


@dataclass
class Manager:
    name: str
    binary: str
    family: str          # used for alternatives install hints
    check: callable
    system: bool         # system package manager (root-owned)


MANAGERS = [
    Manager("apt", "apt-get", "apt", check_apt, True),
    Manager("dnf", "dnf", "dnf", check_dnf, True),
    Manager("yum", "yum", "dnf", check_yum, True),
    Manager("zypper", "zypper", "zypper", check_zypper, True),
    Manager("pacman", "pacman", "pacman", check_pacman, True),
    Manager("flatpak", "flatpak", "flatpak", check_flatpak, False),
    Manager("snap", "snap", "snap", check_snap, False),
    Manager("brew", "brew", "brew", check_brew, False),
    Manager("pip", "python3", "pip", check_pip, False),
    Manager("pipx", "pipx", "pipx", check_pipx, False),
    Manager("cargo", "cargo", "cargo", check_cargo, False),
    Manager("rustup", "rustup", "rustup", check_rustup, False),
    Manager("npm", "npm", "npm", check_npm, False),
]


def available(cfg_managers: dict) -> list[Manager]:
    found = []
    for m in MANAGERS:
        if not cfg_managers.get(m.name, {}).get("enabled", True):
            continue
        if not which(m.binary):
            continue
        # dnf supersedes yum (yum is usually a symlink to dnf)
        if m.name == "yum" and which("dnf"):
            continue
        found.append(m)
    return found


class CommandUnavailable(RuntimeError):
    pass


def update_commands(m: Manager, cfg_managers: dict, use_sudo: bool,
                    pending: dict[str, str] | None = None) -> list[list[str]]:
    """Build argv lists with argv[0] resolved to an absolute, trusted path
    (shown verbatim in the confirmation dialog)."""
    if m.name == "pip":
        pkgs = sorted(p for p in (pending or {}) if _PKG_NAME.match(p))
        if not pkgs:
            return []
        cmds = [[*_pip_base(), "install", "--upgrade", *_pip_scope(), "--", *pkgs]]
    else:
        root = os.geteuid() == 0
        cmds = []
        for c in cfg_managers.get(m.name, {}).get("update", []):
            if not isinstance(c, list) or not all(isinstance(w, str) for w in c):
                raise CommandUnavailable(f"{m.name}: update commands must be lists of strings")
            argv = []
            for w in c:
                if w == "@SUDO":
                    if use_sudo and not root:
                        s = sudo_path()
                        if not s:
                            raise CommandUnavailable("no trusted root-owned setuid sudo found")
                        argv.append(s)
                else:
                    argv.append(w)
            if argv:
                cmds.append(argv)
    out = []
    for argv in cmds:
        exe = argv[0] if os.path.isabs(argv[0]) else which(argv[0])
        if not exe:
            raise CommandUnavailable(f"{argv[0]} not found in a trusted PATH directory")
        out.append([exe, *argv[1:]])
    return out
