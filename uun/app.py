"""Textual TUI."""
from __future__ import annotations

import os
import socket
import subprocess
import time
from datetime import datetime

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, RichLog, Static

from . import __version__, detect, eol, managers, scan
from .safety import SAFE_CWD, child_env, clean
from .config import Config
from .theme import BORDER_MAP, TmuxTheme, list_themes, load_theme

ACTIONS = {  # bind-key targets usable in theme files
    "refresh": "Rescan", "update": "Update", "update_all": "Update all",
    "refresh_eol": "Fetch EoL", "copy": "Copy hint", "next_theme": "Theme",
    "toggle_log": "Log", "help": "Help", "quit": "Quit",
}
DEFAULT_KEYS = {"r": "refresh", "u": "update", "U": "update_all", "e": "refresh_eol",
                "c": "copy", "t": "next_theme", "l": "toggle_log", "question_mark": "help",
                "q": "quit"}

STATE_LABEL = {"EOL": "EOL", "CRIT": "CRIT", "SOON": "SOON", "OK": "OK", "UNKNOWN": "?"}


def _border_css() -> str:
    rules = []
    for b in sorted(set(BORDER_MAP.values())):
        rules.append(f"Screen.b-{b} .pane {{ border: {b} $uun-border; }}")
        rules.append(f"Screen.b-{b} .pane:focus-within {{ border: {b} $uun-border-active; }}")
    return "\n".join(rules)


def _style_dialog(screen: ModalScreen, title: str) -> None:
    d = screen.query_one("#dialog")
    d.border_title = title
    colour = screen.app.current_theme.variables.get("uun-border-active", "#5fafff")
    d.styles.border = (screen.app.tmux.border, colour)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, title: str, commands: list[list[str]]):
        super().__init__()
        self.title_text, self.commands = title, commands

    def compose(self) -> ComposeResult:
        body = Text()
        body.append(self.title_text + "\n\n", style="bold")
        body.append("These exact commands will run (no shell):\n\n")
        for c in self.commands:
            body.append("  $ " + " ".join(c) + "\n", style="bold")
        body.append("\nThe TUI suspends so you can answer sudo / package-manager prompts.\n")
        body.append("[y] run    [n] cancel", style="reverse")
        with Vertical(id="dialog", classes="pane"):
            yield Static(body)

    def on_mount(self) -> None:
        _style_dialog(self, "Confirm update")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss_help", "Close")]

    def __init__(self, keys: dict[str, str], theme: TmuxTheme):
        super().__init__()
        self.keys, self.tmux = keys, theme

    def compose(self) -> ComposeResult:
        t = Text()
        t.append("Keys\n", style="bold underline")
        for k, a in self.keys.items():
            t.append(f"  {('?' if k == 'question_mark' else k):<6}", style="bold")
            t.append(f"{ACTIONS.get(a, a)}\n")
        t.append("  tab   next pane\n\n")
        t.append("States\n", style="bold underline")
        pal = self.tmux.palette
        for s, desc, c in (("EOL", "past end-of-life", "crit"), ("CRIT", "EoL within critical_days", "crit"),
                           ("SOON", "EoL within warn_days", "warn"), ("OK", "supported", "ok"),
                           ("?", "no data / version unknown", "unknown")):
            t.append(f"  {s:<6}", style=f"bold {pal[c]}")
            t.append(desc + "\n")
        t.append("\nTheme: ", style="bold")
        t.append(f"{self.tmux.name}  (~/.config/uun/themes/*.conf, tmux syntax)\n")
        t.append("EoL data: endoflife.date (refresh: uun --refresh-eol / systemd timer)\n")
        if self.tmux.errors:
            t.append("\nTheme warnings:\n", style="bold")
            for e in self.tmux.errors:
                t.append(f"  {e}\n")
        with Vertical(id="dialog", classes="pane"):
            yield Static(t)

    def on_mount(self) -> None:
        _style_dialog(self, f"uun {__version__} help")

    def action_dismiss_help(self) -> None:
        self.dismiss()


class UunApp(App):
    TITLE = "uun"
    CSS = """
    Screen { background: $background; color: $foreground; }
    #topbar { height: 1; background: $uun-status-bg; }
    #titlebar { width: 1fr; height: 1; background: $uun-status-bg; color: $uun-status-fg; padding: 0 1; }
    #clock { width: auto; height: 1; padding: 0 1; background: $uun-clock-bg; color: $uun-clock-fg;
             text-style: bold; }
    #main { height: 1fr; }
    .pane { border-title-color: $uun-title-fg; border-title-style: bold;
            border-subtitle-color: $uun-status-fg; background: $background; }
    #mgr-pane { width: 50; }
    #mgr { overflow-x: hidden; }
    #eol-pane { width: 1fr; }
    #detail-pane { height: 16; }
    #detail { padding: 0 1; }
    #log-pane { height: 7; }
    #log-pane.hidden { display: none; }
    #log { background: $uun-msg-bg; color: $uun-msg-fg; }
    DataTable { background: $background; height: 1fr; }
    #eol { overflow-x: auto; }
    DataTable > .datatable--header { background: $background; color: $uun-title-fg; text-style: bold; }
    Footer { background: $uun-status-bg; }
    Screen.status-top Footer { dock: none; }
    ConfirmScreen, HelpScreen { align: center middle; background: $background 60%; }
    #dialog { width: 84; height: auto; max-height: 90%; padding: 1 2; background: $surface; }
    """ + _border_css()

    def __init__(self, cfg: Config, demo: bool = False):
        super().__init__()
        self.cfg, self.demo = cfg, demo
        self.theme_names = list_themes() or ["default"]
        if cfg.theme not in self.theme_names:
            self.theme_names.insert(0, cfg.theme)
        self.tmux = load_theme(cfg.theme)
        self.mgrs: list[managers.Manager] = []
        self.checks: dict[str, managers.CheckResult] = {}
        self.statuses: list[eol.EolStatus] = []
        self.alts = scan.load_alternatives(cfg.alternatives_file)
        self.family = "apt"
        self.keys = {k: a for k, a in DEFAULT_KEYS.items() if k not in self.tmux.unbinds}
        for k, a in self.tmux.binds.items():
            if a in ACTIONS:
                self.keys = {kk: aa for kk, aa in self.keys.items() if aa != a}
                self.keys[k] = a
        for k, a in self.keys.items():
            self.bind(k, a, description=ACTIONS[a])

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return {"uun-border": "#444444", "uun-border-active": "#5fafff", "uun-status-bg": "#303030",
                "uun-status-fg": "#d0d0d0", "uun-msg-bg": "#1c1c1c", "uun-msg-fg": "#d0d0d0",
                "uun-title-fg": "#5fafff", "uun-crit": "#ff5f5f", "uun-warn": "#ffaf00",
                "uun-ok": "#5faf5f", "uun-unknown": "#8a8a8a",
                "uun-clock-bg": "#5fafff", "uun-clock-fg": "#000000"}

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(id="titlebar")
            yield Static(id="clock")
        with Horizontal(id="main"):
            with Vertical(id="mgr-pane", classes="pane"):
                yield DataTable(id="mgr", cursor_type="row", zebra_stripes=False)
            with Vertical(id="eol-pane", classes="pane"):
                yield DataTable(id="eol", cursor_type="row")
        with VerticalScroll(id="detail-pane", classes="pane"):
            yield Static(id="detail")
        with Vertical(id="log-pane", classes="pane"):
            yield RichLog(id="log", markup=False, wrap=True, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        for pane, title in (("#mgr-pane", "Package managers"), ("#eol-pane", "End-of-life watch"),
                            ("#detail-pane", "Details & alternatives"), ("#log-pane", "Log")):
            self.query_one(pane).border_title = title
        mt = self.query_one("#mgr", DataTable)
        for name, w in (("Manager", 7), ("Pending", 7), ("Sec", 3), ("Status", 24)):
            mt.add_column(name, width=w)
        self.query_one("#eol", DataTable).add_columns(
            "State", "Software", "Installed", "Cycle", "EoL date", "Days", "Inbound", "Upgrade to")
        self.apply_tmux(self.tmux)
        for e in self.tmux.errors:
            self.log_line(f"theme: {e}")
        osr = detect.os_release()
        self.os_name = clean(osr.get("PRETTY_NAME", "unknown OS"), 48)
        like = (osr.get("ID", "") + " " + osr.get("ID_LIKE", "")).split()
        self.family = ("dnf" if {"rhel", "fedora", "centos"} & set(like) else
                       "zypper" if any("suse" in x for x in like) else
                       "pacman" if "arch" in like else "apt")
        self.set_interval(30, self.update_titlebar)
        self.update_titlebar()
        self.tick_clock()
        self.set_interval(1, self.tick_clock)
        self.query_one("#eol", DataTable).focus()
        self.rescan()

    # ------------------------------------------------------------ theming
    def apply_tmux(self, tmux: TmuxTheme) -> None:
        self.tmux = tmux
        th = tmux.to_textual()
        self.register_theme(th)
        self.theme = th.name
        for b in set(BORDER_MAP.values()):
            self.screen.remove_class(f"b-{b}")
        self.screen.add_class(f"b-{tmux.border}")
        self.screen.set_class(tmux.status_top, "status-top")
        # status-position top: key bar sits directly under the title/clock bar
        footer, top = self.query_one(Footer), self.query_one("#topbar")
        if tmux.status_top:
            self.screen.move_child(footer, after=top)
        else:
            self.screen.move_child(footer, after=self.query_one("#log-pane"))
        self.render_eol()
        self.render_detail()

    def action_next_theme(self) -> None:
        i = (self.theme_names.index(self.tmux.name) + 1) % len(self.theme_names) \
            if self.tmux.name in self.theme_names else 0
        self.apply_tmux(load_theme(self.theme_names[i]))
        self.notify(f"theme: {self.tmux.name}", timeout=2, markup=False)
        self.update_titlebar()

    def update_titlebar(self) -> None:
        age = eol.cache_age_hours() if not self.demo else 2.0
        age_s = "never" if age is None else (f"{age:.0f}h ago" if age < 48 else f"{age / 24:.0f}d ago")
        host = "demo-host" if self.demo else socket.gethostname()
        os_name = "Ubuntu 22.04.5 LTS (demo data)" if self.demo else self.os_name
        txt = Text()
        txt.append(f" uun {__version__} ", style="bold reverse")
        txt.append(f"  {host}  │  {os_name}  │  EoL data: {age_s}  │  theme: {self.tmux.name}")
        self.query_one("#titlebar", Static).update(txt)

    def tick_clock(self) -> None:
        """Upper-right clock; format is tmux-style strftime via `set -g @clock-format`."""
        fmt = self.tmux.options.get("@clock-format", "%Y-%m-%d %H:%M:%S")
        try:
            now = datetime.now().astimezone().strftime(fmt)
        except ValueError:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.query_one("#clock", Static).update(now)

    def log_line(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(f"{datetime.now():%H:%M:%S} {msg}")

    # ------------------------------------------------------------ scanning
    def action_refresh(self) -> None:
        self.rescan()

    def action_refresh_eol(self) -> None:
        self.rescan(force_eol=True)

    @work(thread=True, exclusive=True, group="scan")
    def rescan(self, force_eol: bool = False) -> None:
        if self.demo:
            from . import demo
            self.call_from_thread(self._demo_load, demo)
            return
        age = eol.cache_age_hours()
        if force_eol or (self.cfg.auto_refresh_eol and (age is None or age > self.cfg.eol_max_age_hours)):
            self.call_from_thread(self.log_line, "fetching EoL data from endoflife.date …")
            res = eol.refresh(self.cfg.eol_api, scan.all_slugs(self.cfg))
            n_err = len(res.get("errors", {}))
            self.call_from_thread(self.log_line, f"EoL data: {len(res['products'])} products"
                                  + (f", {n_err} failed: {', '.join(list(res['errors'])[:5])}" if n_err else ""))
            self.call_from_thread(self.update_titlebar)
        self.mgrs = managers.available(self.cfg.managers)
        self.call_from_thread(self._mgr_placeholders)
        self.checks = {}
        for name, res in scan.check_all(self.mgrs).items():
            self.checks[name] = res
        self.call_from_thread(self.render_mgrs)
        found = detect.detect(detect.load_specs(self.cfg.products_file))
        self.statuses = scan.evaluate_all(self.cfg, found, self.checks)
        self.call_from_thread(self.render_eol)
        self.call_from_thread(self.render_detail)
        att = sum(s.needs_attention for s in self.statuses)
        self.call_from_thread(self.log_line, f"scan done: {sum(r.count or 0 for r in self.checks.values())} "
                              f"pending updates, {att} EoL item(s) need attention")

    def _demo_load(self, demo) -> None:
        self.mgrs, self.checks = demo.managers(), demo.checks()
        self.statuses = scan.evaluate_all(self.cfg, demo.detected(), self.checks, demo.cache())
        self.render_mgrs()
        self.render_eol()
        self.render_detail()
        for line in demo.LOG:
            self.log_line(line)

    def _mgr_placeholders(self) -> None:
        t = self.query_one("#mgr", DataTable)
        t.clear()
        for m in self.mgrs:
            t.add_row(Text(m.name), Text("…"), Text(""), Text("checking…", style="italic"), key=m.name)

    def render_mgrs(self) -> None:
        t = self.query_one("#mgr", DataTable)
        pal = self.tmux.palette
        t.clear()
        for m in self.mgrs:
            r = self.checks.get(m.name, managers.CheckResult())
            cnt = Text("?" if r.count is None else str(r.count),
                       style=pal["unknown"] if r.count is None else (pal["warn"] if r.count else pal["ok"]))
            sec = Text(str(r.security) if r.security else "", style=f"bold {pal['crit']}")
            msg = r.error or r.note or "ok"
            msg = msg if len(msg) <= 24 else msg[:23] + "…"
            status = Text(msg, style=pal["crit"]) if r.error else Text(msg, style="dim")
            t.add_row(Text(m.name, style="bold"), cnt, sec, status, key=m.name)
        total = sum(r.count or 0 for r in self.checks.values())
        self.query_one("#mgr-pane").border_subtitle = f"{total} pending"

    def render_eol(self) -> None:
        if not self.is_mounted:
            return
        t = self.query_one("#eol", DataTable)
        row = t.cursor_row
        pal = self.tmux.palette
        colour = {"EOL": pal["crit"], "CRIT": pal["crit"], "SOON": pal["warn"],
                  "OK": pal["ok"], "UNKNOWN": pal["unknown"]}
        t.clear()
        for s in self.statuses:
            c = colour[s.state]
            label = Text(f" {STATE_LABEL[s.state]:<4} ", style=f"bold reverse {c}" if s.state in ("EOL", "CRIT")
                         else f"bold {c}")
            days = "" if s.days_left is None else str(s.days_left)
            inbound = (Text("✔ " + s.inbound, style=pal["ok"]) if s.inbound_fixes else
                       Text(s.inbound, style="dim") if s.inbound else
                       Text("none", style=pal["crit"]) if s.needs_attention else Text(""))
            inst = s.installed if len(s.installed) <= 12 else s.installed[:11] + "…"
            # Every cell is a Text: DataTable parses plain str as markup, which
            # would let remote EoL data inject styles or [@click=...] actions.
            t.add_row(label, Text(s.label, style="bold"), Text(inst), Text(s.cycle or "-"),
                      Text(str(s.eol or "-")), Text(days, style=c), inbound,
                      Text(s.upgrade_to or ""), key=s.slug)
        if self.statuses:
            t.move_cursor(row=min(row, len(self.statuses) - 1))
        att = sum(s.needs_attention for s in self.statuses)
        self.query_one("#eol-pane").border_subtitle = f"{att} need attention · {len(self.statuses)} tracked"

    # ------------------------------------------------------------ details
    @on(DataTable.RowHighlighted, "#eol")
    def _eol_moved(self) -> None:
        self.render_detail()

    def selected_status(self) -> eol.EolStatus | None:
        t = self.query_one("#eol", DataTable)
        if not self.statuses or t.cursor_row < 0 or t.cursor_row >= len(self.statuses):
            return None
        return self.statuses[t.cursor_row]

    def render_detail(self) -> None:
        if not self.is_mounted:
            return
        s = self.selected_status()
        pal = self.tmux.palette
        out = Text()
        if s is None:
            out.append("Scanning…" if not self.statuses else "Select a row.", style="dim")
            self.query_one("#detail", Static).update(out)
            return
        c = {"EOL": pal["crit"], "CRIT": pal["crit"], "SOON": pal["warn"], "OK": pal["ok"]}.get(s.state, pal["unknown"])
        out.append(f"{s.label} {s.installed}", style="bold")
        out.append(f"   detected via `{s.source}`\n", style="dim")
        if s.eol:
            when = f"ended {-s.days_left} days ago" if s.days_left < 0 else f"in {s.days_left} days"
            out.append(f"Cycle {s.cycle}: end-of-life {s.eol} ({when})\n", style=c)
        elif s.note:
            out.append(s.note + "\n", style=pal["unknown"])
        else:
            out.append(f"Cycle {s.cycle}: no EoL date announced\n", style=pal["ok"])
        if s.latest_in_cycle and s.latest_in_cycle != s.installed:
            out.append(f"Latest patch in this cycle: {s.latest_in_cycle}\n")
        if s.inbound:
            out.append(f"Inbound repo update: {s.inbound} ", style="bold")
            out.append("(moves to a supported cycle)\n" if s.inbound_fixes else "(same cycle; patch only)\n",
                       style=pal["ok"] if s.inbound_fixes else "dim")
        elif s.needs_attention:
            out.append("No update inbound from your package managers.\n", style=f"bold {pal['crit']}")
        if s.upgrade_to and s.needs_attention:
            out.append(f"Upstream upgrade path: {s.upgrade_to}"
                       + (f" (supported until {s.upgrade_eol})" if s.upgrade_eol else "") + "\n")
        if s.suggest_alternative:
            alts = self.alts.get(s.slug, {}).get("alternatives", [])
            if alts:
                out.append("Suggested alternatives:\n", style=f"bold {pal['warn']}")
                for a in alts:
                    out.append(f"  ▸ {a['name']}", style="bold")
                    out.append(f" — {a.get('why', '')}\n")
                    hint = a.get("install", {}).get(self.family)
                    if hint:
                        out.append(f"      install: sudo {self.family} install {hint}\n", style=pal["ok"])
                    out.append("      ")
                    out.append(a.get("url", ""), style="underline dim")
                    out.append("\n")
        if s.url:
            out.append(f"Lifecycle: {s.url}\n", style="dim")
        self.query_one("#detail", Static).update(out)

    def action_copy(self) -> None:
        s = self.selected_status()
        if not s:
            return
        alts = self.alts.get(s.slug, {}).get("alternatives", [])
        hint = next((f"sudo {self.family} install {a['install'][self.family]}" for a in alts
                     if a.get("install", {}).get(self.family)), None)
        text = hint or (alts[0]["url"] if alts else s.url)
        if text:
            text = clean(text, 300)
            self.copy_to_clipboard(text)
            self.notify(f"copied: {text}", timeout=3, markup=False)

    def action_toggle_log(self) -> None:
        self.query_one("#log-pane").toggle_class("hidden")

    def action_help(self) -> None:
        self.push_screen(HelpScreen(self.keys, self.tmux))

    # ------------------------------------------------------------ updates
    def _cmds_for(self, m: managers.Manager) -> list[list[str]]:
        r = self.checks.get(m.name)
        try:
            return managers.update_commands(m, self.cfg.managers, self.cfg.use_sudo,
                                            r.pending if r else None)
        except managers.CommandUnavailable as e:
            self.log_line(f"{m.name}: not updating: {e}")
            self.notify(f"{m.name}: {e}", severity="error", timeout=5, markup=False)
            return []

    def action_update(self) -> None:
        t = self.query_one("#mgr", DataTable)
        if not self.mgrs:
            return
        if self.focused is not t:
            self.notify("Select a package manager in the left pane (tab), then press u", timeout=3)
            t.focus()
            return
        m = self.mgrs[t.cursor_row]
        self._confirm_and_run(f"Update {m.name}", self._cmds_for(m))

    def action_update_all(self) -> None:
        cmds = [c for m in self.mgrs if (self.checks.get(m.name) or managers.CheckResult()).count != 0
                for c in self._cmds_for(m)]
        self._confirm_and_run("Update everything with pending updates", cmds)

    def _confirm_and_run(self, title: str, cmds: list[list[str]]) -> None:
        if not cmds:
            self.notify("Nothing to run", timeout=2)
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.run_commands(cmds)
        self.push_screen(ConfirmScreen(title, cmds), done)

    def run_commands(self, cmds: list[list[str]]) -> None:
        if self.demo:
            self.log_line("demo mode: would run " + " && ".join(" ".join(c) for c in cmds))
            return
        try:
            with self.suspend():
                for c in cmds:
                    print(f"\n\033[1m$ {' '.join(c)}\033[0m", flush=True)
                    rc = subprocess.call(c, cwd=SAFE_CWD, env=child_env(locale_c=False))
                    if rc != 0:
                        print(f"\n\033[31mcommand failed (exit {rc}); stopping\033[0m")
                        break
                input("\nPress Enter to return to uun… ")
        except SuspendNotSupported:
            self._run_streaming(cmds)
            return
        self.log_line("updates finished: " + " && ".join(" ".join(c) for c in cmds))
        self.rescan()

    @work(thread=True, exclusive=True, group="update")
    def _run_streaming(self, cmds: list[list[str]]) -> None:
        for c in cmds:
            c = [c[0], "-n", *c[1:]] if c and os.path.basename(c[0]) == "sudo" else c
            self.call_from_thread(self.log_line, "$ " + " ".join(c))
            p = subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 errors="replace", stdin=subprocess.DEVNULL, cwd=SAFE_CWD,
                                 env={**child_env(locale_c=False), "DEBIAN_FRONTEND": "noninteractive"})
            for line in p.stdout:
                self.call_from_thread(self.log_line, clean(line.rstrip(), 400))
            if p.wait() != 0:
                self.call_from_thread(self.log_line, f"failed (exit {p.returncode})")
                break
        self.call_from_thread(self.rescan)
