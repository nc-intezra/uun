"""Security helpers: trusted file reads, safe executable resolution, output sanitising.

Threat model: uun runs as a normal user, may invoke sudo, reads data from the
network (endoflife.date) and from local tools, and is often started from
arbitrary working directories (e.g. inside a freshly cloned repo).
"""
from __future__ import annotations

import os
import re
import stat
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
            raise UnsafeFileError(f"{d} is owned by uid {st.st_uid}; {path} could be replaced")
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
            raise UnsafeFileError(f"{path} is owned by uid {st.st_uid}, not you or root")
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
