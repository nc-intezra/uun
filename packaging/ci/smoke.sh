#!/bin/sh
# shellcheck disable=SC2016  # run_as bodies are single-quoted on purpose: they expand in the tester's shell
# Install-test a built package inside a distro container.
#   usage: smoke.sh deb|rpm-dnf|rpm-zypper /path/to/package
# Runs as root in the container; uun itself is exercised as an unprivileged user.
set -eu
kind=$1
pkg=$2
log() { printf '\n== %s\n' "$*"; }

log "install $pkg ($kind)"
case $kind in
    deb)
        export DEBIAN_FRONTEND=noninteractive
        # Fail clearly if the mirrors are unreachable (otherwise apt reports
        # a misleading "held broken packages").
        n=0
        until apt-get update -qq -o APT::Update::Error-Mode=any; do
            n=$((n + 1)); [ "$n" -lt 3 ] || { echo "FAIL: apt mirrors unreachable"; exit 1; }
            sleep 10
        done
        apt-get install -y -qq "$pkg"
        ;;
    rpm-dnf)
        dnf install -y -q "$pkg"
        ;;
    rpm-zypper)
        zypper --non-interactive install --allow-unsigned-rpm "$pkg"
        ;;
esac

run_as() {
    if [ "$(id -u)" = 0 ] && command -v useradd >/dev/null 2>&1 && command -v su >/dev/null 2>&1; then
        id tester >/dev/null 2>&1 || useradd -m tester
        su tester -s /bin/sh -c "$*"
    else
        sh -c "$*"
    fi
}

log "basic commands (unprivileged user)"
run_as '/usr/bin/uun --version'
run_as '/usr/bin/uun --list-themes'

log "hostile working directory + '.' in PATH must not execute local code"
run_as 'set -e; d=$(mktemp -d); cd "$d";
    printf "%s\n" "open(\"$d/PWNED\",\"w\").write(\"x\")" "get_version=lambda:\"9\"" > django.py;
    mkdir pip; cp django.py pip/__main__.py;
    printf "#!/bin/sh\ntouch %s/PWNED\n" "$d" > python3; chmod +x python3;
    PATH=".:$PATH" /usr/bin/uun --report >/dev/null || true;
    test ! -e "$d/PWNED" || { echo "FAIL: code from cwd executed"; exit 1; }
    echo "ok: nothing executed from $d"'

log "fetch EoL data and report (network)"
run_as 'timeout 300 /usr/bin/uun --refresh-eol' || echo "WARN: EoL refresh failed (network?)"
run_as 'rc=0; /usr/bin/uun --report || rc=$?; case $rc in 0|1|2) echo "report exit $rc (ok)";; *) exit "$rc";; esac'

log "headless TUI smoke test with the bundled libraries"
for py in /usr/bin/python3 /usr/bin/python3.11 /usr/bin/python3.12 /usr/bin/python3.13 /usr/bin/python3.14; do
    [ -x "$py" ] && "$py" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && break
    py=
done
[ -n "$py" ] || { echo "FAIL: no Python >= 3.11 found after install"; exit 1; }
"$py" -I - <<'PY'
import asyncio, sys
sys.path.insert(0, "/usr/lib/uun")
import rich, textual, uun
for m in (uun, textual, rich):
    assert m.__file__.startswith("/usr/lib/uun/"), f"{m.__name__} not from bundle: {m.__file__}"
from uun.app import UunApp
from uun.config import load_config

async def main():
    app = UunApp(load_config(), demo=True)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("t")
        await pilot.pause(0.3)
        assert len(app.statuses) == 10, app.statuses
        clock = str(app.query_one("#clock").render())
        assert len(clock.strip()) == 19, clock
    print(f"ok: python {sys.version.split()[0]}, textual {textual.__version__}, theme {app.tmux.name}")
asyncio.run(main())
PY

log "uninstall"
case $kind in
    deb) apt-get purge -y -qq uun ;;
    rpm-dnf) dnf remove -y -q uun ;;
    rpm-zypper) zypper --non-interactive remove uun ;;
esac
test ! -e /usr/bin/uun || { echo "FAIL: /usr/bin/uun left behind"; exit 1; }
test ! -e /usr/lib/uun || { echo "FAIL: /usr/lib/uun left behind"; find /usr/lib/uun | head; exit 1; }
log "PASS"
