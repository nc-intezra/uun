"""Security helpers: trusted file reads, safe executable resolution, output sanitising.

Threat model: uun runs as a normal user, may invoke sudo, reads data from the
network (endoflife.date) and from local tools, and is often started from
arbitrary working directories (e.g. inside a freshly cloned repo).
"""
from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

# Every child process runs here. "/" is root-owned, so cwd-relative lookups
# (python's sys.path[0], ./.npmrc, ./.cargo/config.toml, ./pip/...) can't be
# planted by another user or by a hostile checkout the user happens to be in.
SAFE_CWD = "/"

MAX_CONFIG_BYTES = 1_000_000


class UnsafeFileError(RuntimeError):
    pass


def _trusted_owner(uid: int) -> bool:
    return uid in (0, os.getuid())


def _owner_hint(uid: int) -> str:
    """Explain the common confusing case: inside a user namespace where root
    isn't mapped (e.g. a systemd user unit using ProtectSystem=/PrivateTmp=),
    root-owned files show up as the overflow uid (usually 65534, 'nobody')."""
    try:
        overflow = int(Path("/proc/sys/kernel/overflowuid").read_text())
        root_mapped = False
        for line in Path("/proc/self/uid_map").read_text().splitlines():
            _inside, outside, count = (int(x) for x in line.split())
            if outside <= 0 < outside + count:   # host uid 0 is visible here
                root_mapped = True
    except (OSError, ValueError):
        return ""
    if uid == overflow and not root_mapped:
        return (" (uun is running in a user namespace where root is unmapped, so root-owned "
                "files appear as uid %d; if this is a systemd user unit, remove "
                "ProtectSystem=/PrivateTmp=/PrivateUsers= from it)" % overflow)
    return ""


def _check_dir_chain(path: Path) -> None:
    """Every ancestor must be owned by us/root and not writable by others
    (sticky world-writable dirs like /tmp are tolerated only above our files'
    owner-controlled directories, i.e. they can't be the immediate parent)."""
    parent = path.parent
    first = True
    for d in [parent, *parent.parents]:
        st = d.stat()
        writable = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        sticky = st.st_mode & stat.S_ISVTX
        if not _trusted_owner(st.st_uid):
            raise UnsafeFileError(f"{d} is owned by uid {st.st_uid}; {path} could be replaced"
                                  f"{_owner_hint(st.st_uid)}")
        if writable and (first or not sticky):
            raise UnsafeFileError(f"{d} is group/world-writable; {path} could be replaced "
                                  f"(run: chmod go-w '{d}')")
        first = False


def read_trusted(path: Path, limit: int = MAX_CONFIG_BYTES) -> str:
    """Read a file that may define commands run under sudo.

    Resolves symlinks once, then opens the resolved path with O_NOFOLLOW and
    validates the *open descriptor* (no stat/read race), plus the directory
    chain (so a writable parent can't be used to swap the file).
    """
    real = Path(os.path.realpath(path))
    _check_dir_chain(real)
    fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeFileError(f"{path} is not a regular file")
        if not _trusted_owner(st.st_uid):
            raise UnsafeFileError(f"{path} is owned by uid {st.st_uid}, not you or root"
                                  f"{_owner_hint(st.st_uid)}")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise UnsafeFileError(f"{path} is group/world-writable; run: chmod go-w '{path}'")
        if st.st_size > limit:
            raise UnsafeFileError(f"{path} is larger than {limit} bytes")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(limit + 1)
    finally:
        os.close(fd)
    return data.decode("utf-8")


# ------------------------------------------------------------- executables

def trusted_dirs(path_var: str | None = None, extra: tuple[str, ...] = ()) -> list[str]:
    """PATH entries that are absolute, existing, owned by us/root and not
    writable by group/other. Drops '', '.', relative entries and e.g. a
    world-writable /tmp/bin."""
    raw = (os.environ.get("PATH", "") if path_var is None else path_var).split(os.pathsep)
    out: list[str] = []
    for d in [*raw, *extra]:
        if not d or not os.path.isabs(d) or d in out:
            continue
        try:
            st = os.stat(d)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or not _trusted_owner(st.st_uid):
            continue
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            continue
        if os.geteuid() == 0 and st.st_uid != 0:
            continue  # as root, only root-owned dirs
        out.append(d)
    return out


def which(cmd: str, dirs: list[str] | None = None) -> str | None:
    if not cmd or "/" in cmd:
        return cmd if cmd and os.path.isabs(cmd) and os.access(cmd, os.X_OK) else None
    for d in dirs if dirs is not None else trusted_dirs():
        p = os.path.join(d, cmd)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and os.access(p, os.X_OK) and _trusted_owner(st.st_uid) \
                and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return p
    return None


def sudo_path() -> str | None:
    """sudo must be a root-owned setuid binary in a root-owned directory, so a
    planted `sudo` can't harvest the password."""
    for d in ("/usr/bin", "/bin", "/usr/local/bin", "/run/wrappers/bin", "/usr/sbin", "/sbin"):
        p = os.path.join(d, "sudo")
        try:
            st, dst = os.stat(p), os.stat(d)
        except OSError:
            continue
        if st.st_uid == 0 and dst.st_uid == 0 and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) \
                and not dst.st_mode & (stat.S_IWGRP | stat.S_IWOTH) \
                and (st.st_mode & stat.S_ISUID or d == "/run/wrappers/bin"):
            return p
    return None


def child_env(extra_dirs: tuple[str, ...] = (), locale_c: bool = True) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(trusted_dirs(extra=extra_dirs)) or "/usr/bin:/bin"
    if locale_c:
        env["LC_ALL"] = "C"
    return env


# ------------------------------------------------------------- sanitising

# C0/C1 controls (ESC → terminal escape / OSC 52 clipboard injection), DEL,
# zero-width and bidi overrides (Trojan-Source style spoofing).
_UNSAFE = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069\\ufeff]")


def clean(value, limit: int = 120) -> str:
    """Make untrusted text safe to print to a terminal. Not a markup escaper:
    the TUI always wraps text in rich.text.Text so brackets stay literal."""
    s = _UNSAFE.sub("", str(value)).replace("\n", " ").replace("\t", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HTTPS = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?$")


def clean_date(v) -> str | None:
    return v if isinstance(v, str) and _DATE.match(v) else None


def clean_url(v) -> str | None:
    return v if isinstance(v, str) and len(v) < 300 and _HTTPS.match(v) else None


# ------------------------------------------------------------- background jobs
# Read-only checks/probes run in their own process group and are tracked, so
# quitting the TUI can stop them (and anything they spawned) immediately instead
# of waiting for up to their full timeout. Package *updates* never go through
# here: interrupting apt/dnf mid-install could damage the system.

SHUTDOWN = threading.Event()
_children: set = set()
_children_lock = threading.Lock()


def _signal_group(p: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_tracked(argv: list[str], timeout: float, env: dict) -> tuple[int, str, str]:
    """subprocess.run() equivalent for background checks: cancellable on exit,
    and a timeout kills the whole process group (helpers like apt's http
    methods included), not just the direct child."""
    if SHUTDOWN.is_set():
        return 130, "", "cancelled"
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, cwd=SAFE_CWD, env=env, text=True,
                         errors="replace", start_new_session=True)
    with _children_lock:
        _children.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
        if SHUTDOWN.is_set():
            return 130, "", "cancelled"
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        _signal_group(p, signal.SIGKILL)
        p.communicate()
        return 124, "", f"timed out after {timeout:g}s"
    finally:
        with _children_lock:
            _children.discard(p)


def stop_background(grace: float = 2.0) -> int:
    """Cancel pending checks and terminate running ones. Returns how many
    process groups were signalled."""
    SHUTDOWN.set()
    with _children_lock:
        procs = list(_children)
    for p in procs:
        _signal_group(p, signal.SIGTERM)
    deadline = time.monotonic() + grace
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        # The group leader exiting doesn't mean its children did (e.g. a tool
        # that ignores SIGTERM); finish the whole group off.
        _signal_group(p, signal.SIGKILL)
    return len(procs)
