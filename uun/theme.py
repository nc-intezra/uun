"""tmux-style theme files -> Textual Theme.

Supported directives (same syntax as ~/.tmux.conf):

    set -g status-style "bg=colour235,fg=colour250"
    set-option -g pane-border-lines rounded
    set -g @eol-critical "#ff5f5f"        # user options start with @, like tmux
    source-file ~/.config/uun/themes/base.conf
    bind-key r refresh
    unbind-key q

Colours: colour0-255 / color0-255, #rrggbb, tmux names (red, brightblue, ...),
and "default" (leave unset).
"""
from __future__ import annotations

import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path

from textual.theme import BUILTIN_THEMES, Theme

from .config import USER_THEME_DIR, data_path

_BASE16 = ["#000000", "#800000", "#008000", "#808000", "#000080", "#800080",
           "#008080", "#c0c0c0", "#808080", "#ff0000", "#00ff00", "#ffff00",
           "#0000ff", "#ff00ff", "#00ffff", "#ffffff"]
_NAMES = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]


def xterm256(n: int) -> str:
    if n < 16:
        return _BASE16[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        r, g, b = steps[n // 36], steps[(n // 6) % 6], steps[n % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def parse_colour(value: str) -> str | None:
    v = value.strip().lower()
    if v in ("default", "terminal", ""):
        return None
    if v.startswith("#") and len(v) == 7:
        int(v[1:], 16)
        return v
    for prefix in ("colour", "color"):
        if v.startswith(prefix) and v[len(prefix):].isdigit():
            return xterm256(min(int(v[len(prefix):]), 255))
    if v.startswith("bright") and v[6:] in _NAMES:
        return _BASE16[8 + _NAMES.index(v[6:])]
    if v in _NAMES:
        return _BASE16[_NAMES.index(v)]
    raise ValueError(f"unknown colour {value!r}")


def parse_style(value: str) -> dict:
    """'bg=colour235,fg=white,bold' -> {'bg': '#262626', 'fg': '#c0c0c0', 'attrs': ['bold']}"""
    out: dict = {"attrs": []}
    for part in value.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            k = {"bg": "bg", "fg": "fg", "background": "bg", "foreground": "fg"}.get(k, k)
            c = parse_colour(v)
            if c:
                out[k] = c
        elif part in ("bold", "italic", "underscore", "reverse", "dim", "none", "nobold"):
            out["attrs"].append({"underscore": "underline"}.get(part, part))
    return out


MAX_THEME_BYTES = 256 * 1024

BORDER_MAP = {"single": "solid", "double": "double", "heavy": "heavy",
              "rounded": "round", "simple": "ascii", "ascii": "ascii", "none": "none",
              "solid": "solid", "round": "round", "tall": "tall", "wide": "wide"}

DEFAULTS = {
    "@textual-base": "textual-dark",
    "pane-border-lines": "rounded",
    "status-position": "bottom",
}


@dataclass
class TmuxTheme:
    name: str
    options: dict = field(default_factory=lambda: dict(DEFAULTS))
    binds: dict = field(default_factory=dict)
    unbinds: set = field(default_factory=set)
    errors: list = field(default_factory=list)

    def style(self, key: str) -> dict:
        return parse_style(self.options[key]) if key in self.options else {"attrs": []}

    def colour(self, key: str, fallback: str) -> str:
        try:
            return parse_colour(self.options.get(key, "")) or fallback
        except ValueError:
            return fallback

    @property
    def border(self) -> str:
        return BORDER_MAP.get(self.options.get("pane-border-lines", "rounded"), "round")

    @property
    def status_top(self) -> bool:
        return self.options.get("status-position") == "top"

    def to_textual(self) -> Theme:
        base = BUILTIN_THEMES.get(self.options.get("@textual-base", "textual-dark"),
                                  BUILTIN_THEMES["textual-dark"])
        win = self.style("window-style")
        status = self.style("status-style")
        border = self.style("pane-border-style")
        active = self.style("pane-active-border-style")
        mode = self.style("mode-style")
        msg = self.style("message-style")
        title = self.style("@title-style")

        primary = self.colour("@accent", active.get("fg") or base.primary)
        fg = win.get("fg") or base.foreground
        bg = win.get("bg") or base.background

        variables = dict(base.variables)
        variables.update({
            "uun-border": border.get("fg") or base.panel or "#444444",
            "uun-border-active": active.get("fg") or primary,
            "uun-status-bg": status.get("bg") or base.panel or "#303030",
            "uun-status-fg": status.get("fg") or fg or "#d0d0d0",
            "uun-msg-bg": msg.get("bg") or bg or "#1c1c1c",
            "uun-msg-fg": msg.get("fg") or fg or "#d0d0d0",
            "uun-title-fg": title.get("fg") or primary,
            "uun-crit": self.colour("@eol-critical", "#ff5f5f"),
            "uun-warn": self.colour("@eol-warning", "#ffaf00"),
            "uun-ok": self.colour("@eol-ok", "#5faf5f"),
            "uun-unknown": self.colour("@eol-unknown", "#8a8a8a"),
        })
        clock = self.style("@clock-style")
        key_s = self.style("@status-key-style")
        variables["uun-clock-bg"] = clock.get("bg") or key_s.get("bg") or primary
        variables["uun-clock-fg"] = clock.get("fg") or key_s.get("fg") or (bg or "#000000")
        if status.get("bg"):
            variables["footer-background"] = status["bg"]
            variables["footer-item-background"] = status["bg"]
            variables["footer-description-background"] = status["bg"]
        if status.get("fg"):
            variables["footer-description-foreground"] = status["fg"]
        key = self.style("@status-key-style")
        if key.get("fg"):
            variables["footer-key-foreground"] = key["fg"]
        if key.get("bg"):
            variables["footer-key-background"] = key["bg"]
        if mode.get("bg"):
            variables["block-cursor-background"] = mode["bg"]
        if mode.get("fg"):
            variables["block-cursor-foreground"] = mode["fg"]

        return Theme(
            name=f"uun-{self.name}",
            primary=primary,
            secondary=base.secondary,
            accent=self.colour("@accent2", base.accent or primary),
            warning=variables["uun-warn"],
            error=variables["uun-crit"],
            success=variables["uun-ok"],
            foreground=fg,
            background=bg,
            surface=self.colour("@surface", base.surface or bg),
            panel=self.colour("@panel", base.panel or bg),
            dark=self.options.get("@dark", "on" if base.dark else "off") != "off",
            variables=variables,
        )

    @property
    def palette(self) -> dict:
        v = self.to_textual().variables
        return {k[4:]: v[k] for k in ("uun-crit", "uun-warn", "uun-ok", "uun-unknown")}


def theme_dirs() -> list[Path]:
    return [USER_THEME_DIR, data_path("themes")]


def find_theme(name: str) -> Path | None:
    p = Path(name).expanduser()
    try:
        if p.suffix == ".conf" and p.exists():
            return p
    except OSError:
        return None
    for d in theme_dirs():
        f = d / f"{name}.conf"
        try:
            if f.exists():
                return f
        except OSError:
            continue  # unreadable theme dir (e.g. foreign XDG_CONFIG_HOME): skip it
    return None


def list_themes() -> list[str]:
    names: set[str] = set()
    for d in theme_dirs():
        try:
            if d.is_dir():
                names.update(f.stem for f in d.glob("*.conf"))
        except OSError:
            continue
    return sorted(names)


def load_theme(name: str) -> TmuxTheme:
    theme = TmuxTheme(name=Path(name).stem)
    path = find_theme(name)
    if path is None:
        theme.errors.append(f"theme {name!r} not found; using built-in defaults")
        return theme
    _parse_file(theme, path, depth=0)
    return theme


def _parse_file(theme: TmuxTheme, path: Path, depth: int) -> None:
    if depth > 8:
        theme.errors.append(f"{path}: source-file nesting too deep")
        return
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_THEME_BYTES:
            raise ValueError("not a regular file or larger than 256 KiB")
        text = path.read_text(errors="replace")
    except (OSError, ValueError) as e:
        theme.errors.append(f"{path}: {e}")
        return
    for lineno, raw in enumerate(text.splitlines(), 1):
        try:
            words = shlex.split(raw, comments=True)
        except ValueError as e:
            theme.errors.append(f"{path}:{lineno}: {e}")
            continue
        if not words:
            continue
        cmd, args = words[0], [w for w in words[1:] if not w.startswith("-") or w == "-"]
        flags = {c for w in words[1:] if w.startswith("-") and w != "-" for c in w[1:]}
        try:
            if cmd in ("set", "set-option", "setw", "set-window-option"):
                if len(args) < 2:
                    if "u" in flags and args:
                        theme.options.pop(args[0], None)
                        continue
                    raise ValueError("expected: set -g <option> <value>")
                opt, val = args[0], " ".join(args[1:])
                if "a" in flags and opt in theme.options:
                    val = theme.options[opt] + "," + val
                if opt.endswith("-style") or opt.startswith("@") and "style" in opt:
                    parse_style(val)  # validate early
                theme.options[opt] = val
            elif cmd in ("source-file", "source"):
                target = Path(args[0]).expanduser()
                if not target.is_absolute():
                    target = path.parent / target
                try:
                    present = target.exists()
                except OSError as e:
                    raise ValueError(f"cannot access {target}: {e.strerror or e}") from None
                if present:
                    _parse_file(theme, target, depth + 1)
                elif "q" not in flags:
                    raise ValueError(f"no such file {target}")
            elif cmd in ("bind-key", "bind"):
                theme.binds[args[0]] = args[1].replace("-", "_")
            elif cmd in ("unbind-key", "unbind"):
                theme.unbinds.add(args[0])
            else:
                raise ValueError(f"unknown command {cmd!r}")
        except (ValueError, IndexError) as e:
            theme.errors.append(f"{path.name}:{lineno}: {e}")
