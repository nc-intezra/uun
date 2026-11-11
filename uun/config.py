"""Paths, configuration loading and file-safety checks."""
from __future__ import annotations

import copy
import os
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


from .safety import UnsafeFileError, read_trusted  # noqa: E402  (re-exported)


class ConfigError(RuntimeError):
    """A config location exists but can't be accessed. Fatal on purpose:
    silently falling back to defaults could drop settings such as use_sudo."""


def _xdg_hint(path: Path) -> str:
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        val = os.environ.get(var)
        if val and str(path).startswith(val.rstrip("/") + "/"):
            return (f" ({var}={val} — is it pointing at another user's directory? "
                    f"unset it or point it at your own)")
    return ""


def _access_error(path: Path, e: OSError) -> ConfigError:
    return ConfigError(f"cannot access {path}: {e.strerror or e}{_xdg_hint(path)}")


def _present(path: Path) -> bool:
    """exists() that turns 'permission denied' into a clear ConfigError
    instead of a traceback (missing files are normal and return False)."""
    try:
        return path.exists()
    except OSError as e:
        raise _access_error(path, e) from None


def _read(path: Path) -> str:
    try:
        return read_trusted(path)
    except UnsafeFileError:
        raise
    except OSError as e:
        raise _access_error(path, e) from None


def assert_safe(path: Path) -> str:
    """Validate and read a file that may define commands run under sudo.
    See safety.read_trusted (fd-based checks + directory chain, no TOCTOU)."""
    return read_trusted(path)


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
    defaults = tomllib.loads(read_trusted(data_path("config.default.toml")))
    path = path or CONFIG_FILE
    user: dict = {}
    if _present(path):
        user = tomllib.loads(_read(path))
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
        if _present(user_file):
            _read(user_file)  # fail early; readers re-validate on every read
            setattr(cfg, attr, user_file)
        else:
            setattr(cfg, attr, data_path(default))
    return cfg
