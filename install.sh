#!/usr/bin/env bash
# Install uun for the current user (no root needed) and enable the daily EoL refresh.
#   ./install.sh            install + systemd user timer (falls back to cron)
#   ./install.sh --no-timer install only
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "install: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] && echo "install: note: installing for root; consider a normal user (uun calls sudo itself)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python >= 3.11 required"

if command -v pipx >/dev/null; then
  pipx install --force .
else
  echo "install: pipx not found, using a private venv in ~/.local/share/uun"
  python3 -m venv "$HOME/.local/share/uun/venv"
  "$HOME/.local/share/uun/venv/bin/pip" install -q --upgrade pip .
  mkdir -p "$HOME/.local/bin"
  ln -sf "$HOME/.local/share/uun/venv/bin/uun" "$HOME/.local/bin/uun"
fi

cfg="${XDG_CONFIG_HOME:-$HOME/.config}/uun"
mkdir -p "$cfg/themes"
chmod 700 "$cfg"
if [[ ! -e $cfg/config.toml ]]; then
  install -m 600 uun/data/config.default.toml "$cfg/config.toml"
  echo "install: wrote $cfg/config.toml"
fi

if [[ ${1:-} != --no-timer ]]; then
  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$unit" "${XDG_CACHE_HOME:-$HOME/.cache}/uun"
    install -m 644 systemd/uun-eol-refresh.{service,timer} "$unit/"
    systemctl --user daemon-reload
    systemctl --user enable --now uun-eol-refresh.timer
    echo "install: enabled systemd user timer (systemctl --user list-timers uun*)"
  else
    line="17 */12 * * * $HOME/.local/bin/uun --refresh-eol >/dev/null 2>&1"
    ( crontab -l 2>/dev/null | grep -v 'uun --refresh-eol'; echo "$line" ) | crontab -
    echo "install: added crontab entry (every 12h)"
  fi
fi

"$HOME/.local/bin/uun" --refresh-eol || echo "install: EoL fetch failed (offline?) — the TUI will retry"
echo "install: done. Run: uun   (or: uun --demo to preview themes safely)"
