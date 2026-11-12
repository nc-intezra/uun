#!/usr/bin/env bash
# Build release artifacts for uun from a source checkout:
#   wheel + sdist, .deb (Ubuntu/Debian/Raspberry Pi OS), .rpm (RHEL/Fedora/openSUSE),
#   SHA256SUMS.
#
# usage: packaging/build.sh <source-checkout> <out-dir>
#   The packaging recipe (this directory) may come from a newer commit than the
#   source being released; the source tree is never modified.
# needs: python3 (>= 3.11) with pip + build, dpkg-deb, rpmbuild (optional: skipped if absent)
set -euo pipefail

SRC=$(realpath "$1")
OUT=$(realpath -m "$2")
PKG=$(dirname "$(realpath "$0")")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

VERSION=$(python3 -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$SRC/pyproject.toml")
EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$SRC" log -1 --format=%ct 2>/dev/null || date +%s)}
export SOURCE_DATE_EPOCH=$EPOCH
mkdir -p "$OUT"
echo "== uun $VERSION (SOURCE_DATE_EPOCH=$EPOCH)"

# 1. wheel + sdist from a pristine copy (setuptools writes build/ and *.egg-info
#    into the tree it builds, which would leak into later builds)
cp -a "$SRC" "$WORK/src"
rm -rf "$WORK/src/build" "$WORK/src/"*.egg-info "$WORK/src/.git"
python3 -m build --sdist --wheel --outdir "$OUT" "$WORK/src"
# setuptools doesn't clamp sdist mtimes; normalise so the sdist is reproducible
python3 - "$OUT/uun-$VERSION.tar.gz" "$EPOCH" <<'PY'
import gzip, io, sys, tarfile
path, epoch = sys.argv[1], int(sys.argv[2])
with tarfile.open(path) as src:
    members = [(m, src.extractfile(m).read() if m.isfile() else None) for m in src.getmembers()]
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as dst:
    for m, data in sorted(members, key=lambda x: x[0].name):
        m.mtime = min(m.mtime, epoch)
        m.uid = m.gid = 0
        m.uname = m.gname = ""
        m.pax_headers = {}
        dst.addfile(m, io.BytesIO(data) if data is not None else None)
with open(path, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as gz:
    gz.write(buf.getvalue())
PY

# 2. bundled dependencies: exact audited versions, hash-checked, pure-Python only
python3 -m pip download --quiet --require-hashes --no-deps --only-binary=:all: \
    --python-version 3.11 --implementation py --abi none --platform any \
    -r "$PKG/requirements.lock" -d "$WORK/wheels"
cp "$OUT/uun-$VERSION-py3-none-any.whl" "$WORK/wheels/"
for w in "$WORK"/wheels/*.whl; do
    case "$w" in *-py3-none-any.whl|*-py2.py3-none-any.whl) ;; *) echo "non-pure wheel: $w" >&2; exit 1;; esac
done

# 3. staged root tree shared by deb and rpm
R="$WORK/root"
mkdir -p "$R/usr/lib/uun" "$R/usr/bin" "$R/usr/lib/systemd/user" "$R/usr/share/doc/uun"
# Pure wheels are installed by unpacking them (deterministic: no installer
# metadata such as direct_url.json pointing at temp paths).
python3 - "$R/usr/lib/uun" "$WORK"/wheels/*.whl <<'PY'
import sys, zipfile
dest = sys.argv[1]
for whl in sys.argv[2:]:
    with zipfile.ZipFile(whl) as z:
        for n in z.namelist():
            if n.startswith("/") or ".." in n.split("/") or ".data/" in n:
                sys.exit(f"refusing unexpected wheel member {n!r} in {whl}")
        z.extractall(dest)
PY
find "$R/usr/lib/uun" -name __pycache__ -prune -exec rm -rf {} +
# Optional offline baseline: a signed EoL bundle (and its .sig) shipped in
# the package and imported on first run when nothing newer is cached.
if [ -n "${UUN_EOL_BASELINE:-}" ]; then
    [ -f "$UUN_EOL_BASELINE" ] && [ -f "$UUN_EOL_BASELINE.sig" ] \
        || { echo "UUN_EOL_BASELINE: need $UUN_EOL_BASELINE and its .sig" >&2; exit 1; }
    install -m 644 "$UUN_EOL_BASELINE" "$R/usr/lib/uun/uun/data/eol-baseline.json"
    install -m 644 "$UUN_EOL_BASELINE.sig" "$R/usr/lib/uun/uun/data/eol-baseline.json.sig"
    echo "== embedded EoL baseline $(basename "$UUN_EOL_BASELINE")"
fi
install -m 755 "$PKG/launcher.sh" "$R/usr/bin/uun"
sed -e 's#^ExecStart=.*#ExecStart=/usr/bin/uun --refresh-eol#' \
    "$SRC/systemd/uun-eol-refresh.service" > "$R/usr/lib/systemd/user/uun-eol-refresh.service"
cp "$SRC/systemd/uun-eol-refresh.timer" "$R/usr/lib/systemd/user/"
cp "$SRC/README.md" "$SRC/SECURITY.md" "$SRC/LICENSE" "$R/usr/share/doc/uun/"
find "$R" -type d -exec chmod 755 {} +
find "$R/usr" -type f ! -path '*/usr/bin/*' -exec chmod 644 {} +
find "$R" -exec touch -h -d "@$EPOCH" {} +

# 4. .deb
D="$WORK/deb"
cp -a "$R" "$D"
mkdir -p "$D/DEBIAN"
cp "$PKG/deb/copyright" "$D/usr/share/doc/uun/copyright"
rm -f "$D/usr/share/doc/uun/LICENSE"   # Debian policy: licence lives in copyright
printf 'uun (%s-1) unstable; urgency=medium\n\n  * Release %s.\n\n -- nc-intezra <nc-intezra@users.noreply.github.com>  %s\n' \
    "$VERSION" "$VERSION" "$(date -u -R -d "@$EPOCH")" | gzip -9n > "$D/usr/share/doc/uun/changelog.Debian.gz"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$(du -sk "$D/usr" | cut -f1)/" "$PKG/deb/control.in" > "$D/DEBIAN/control"
install -m 755 "$PKG/deb/postinst" "$PKG/deb/prerm" "$D/DEBIAN/"
( cd "$D" && find usr -type f -exec md5sum {} + | sort -k2 > DEBIAN/md5sums )
find "$D" -exec touch -h -d "@$EPOCH" {} +
dpkg-deb --root-owner-group -Zxz --build "$D" "$OUT/uun_${VERSION}-1_all.deb"

# 5. .rpm
if command -v rpmbuild >/dev/null; then
    T="$WORK/rpmbuild"
    mkdir -p "$T"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}
    tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$EPOCH" -C "$R" -czf "$T/SOURCES/uun-root.tar.gz" usr
    sed -e "s/@VERSION@/$VERSION/" -e "s/@CHANGELOG_DATE@/$(LC_ALL=C date -u -d "@$EPOCH" '+%a %b %d %Y')/" \
        "$PKG/rpm/uun.spec" > "$T/SPECS/uun.spec"
    rpmbuild --quiet -bb --define "_topdir $T" --define "uun_version $VERSION" \
        --define "use_source_date_epoch_as_buildtime 1" --define "clamp_mtime_to_source_date_epoch 1" \
        "$T/SPECS/uun.spec"
    cp "$T"/RPMS/noarch/uun-"$VERSION"-1.noarch.rpm "$OUT/"
else
    echo "rpmbuild not found: skipping .rpm" >&2
fi

# 6. checksums
files=("uun-$VERSION.tar.gz" "uun-$VERSION-py3-none-any.whl" "uun_${VERSION}-1_all.deb")
[ -f "$OUT/uun-$VERSION-1.noarch.rpm" ] && files+=("uun-$VERSION-1.noarch.rpm")
( cd "$OUT" && sha256sum "${files[@]}" > SHA256SUMS )
cat "$OUT/SHA256SUMS"
