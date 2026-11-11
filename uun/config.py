"""Paths, configuration loading and file-safety checks."""
from __future__ import annotations

import copy
import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

APP = "uun"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default) / APP


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")
CONFIG_FILE = CONFIG_DIR / "config.toml"
USER_THEME_DIR = CONFIG_DIR / "themes"
EOL_CACHE = CACHE_DIR / "eol.json"


def data_path(name: str) -> Path:
    return Path(str(resources.files("uun") / "data" / name))


class UnsafeFileError(RuntimeError):
    pass


def assert_safe(path: Path) -> None:
    """Refuse files that other users could have tampered with.

    config.toml / products.toml contain commands that may run under sudo, so a
    group- or world-writable file (or one owned by another non-root user) is a
    privilege-escalation vector.
    """
    st = path.stat()
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeFileError(f"{path} is group/world-writable; run: chmod go-w '{path}'")
    if st.st_uid not in (0, os.getuid()):
        raise UnsafeFileError(f"{path} is owned by uid {st.st_uid}, not you or root")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    theme: str = "default"
    warn_days: int = 180
    critical_days: int = 60
    eol_max_age_hours: int = 24
    auto_refresh_eol: bool = True
    use_sudo: bool = True
    eol_api: str = "https://endoflife.date/api/v1/products"
    managers: dict = field(default_factory=dict)
    products_file: Path | None = None
    alternatives_file: Path | None = None
    raw: dict = field(default_factory=dict)


def load_config(path: Path | None = None) -> Config:
    defaults = tomllib.loads(data_path("config.default.toml").read_text())
    path = path or CONFIG_FILE
    user: dict = {}
    if path.exists():
        assert_safe(path)
        user = tomllib.loads(path.read_text())
    merged = _merge(defaults, user)
    g = merged.get("general", {})
    cfg = Config(
        theme=g.get("theme", "default"),
        warn_days=int(g.get("warn_days", 180)),
        critical_days=int(g.get("critical_days", 60)),
        eol_max_age_hours=int(g.get("eol_max_age_hours", 24)),
        auto_refresh_eol=bool(g.get("auto_refresh_eol", True)),
        use_sudo=bool(g.get("use_sudo", True)),
        eol_api=g.get("eol_api", Config.eol_api),
        managers=merged.get("managers", {}),
        raw=merged,
    )
    if not cfg.eol_api.startswith("https://"):
        sys.exit("uun: eol_api must be an https:// URL")
    for key, attr, default in (("products", "products_file", "products.toml"),
                               ("alternatives", "alternatives_file", "alternatives.toml")):
        user_file = CONFIG_DIR / default
        if user_file.exists():
            assert_safe(user_file)
            setattr(cfg, attr, user_file)
        else:
            setattr(cfg, attr, data_path(default))
    return cfg
