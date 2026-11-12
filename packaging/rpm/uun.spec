# RPM for RHEL/Rocky/Alma 8-10, Fedora and openSUSE. Built by packaging/build.sh
# from a pre-staged root tree (Source0), so the RPM and .deb ship identical files.
%global debug_package %{nil}
%global __os_install_post %{nil}
%global __brp_mangle_shebangs %{nil}
%define _build_id_links none

Name:           uun
Version:        %{uun_version}
Release:        1
Summary:        Universal update notifier TUI with end-of-life tracking
License:        MIT AND BSD-2-Clause AND PSF-2.0
URL:            https://github.com/nc-intezra/uun
Source0:        uun-root.tar.gz
BuildArch:      noarch
# Bundled pure-Python deps live in /usr/lib/uun; don't let the dependency
# generators invent python3dist() requirements for them.
AutoReqProv:    no
# Any Python >= 3.11: default python3 (Fedora, RHEL 10) or the parallel
# AppStream/SLE packages (RHEL/Rocky/Alma 8-9: python3.11/3.12; openSUSE: python311...).
Requires:       ((python3 >= 3.11) or python3.11 or python3.12 or python3.13 or python311 or python312 or python313)
Requires:       /bin/sh
Recommends:     sudo
Recommends:     openssh-clients

%description
One terminal UI to run updates across dnf, yum, zypper, apt, pacman,
flatpak, snap, Homebrew, pip, pipx, cargo, rustup and npm, and to track
end-of-life dates for the OS, kernel, runtimes and servers using
endoflife.date data. Suggests open-source alternatives when software is
EoL with no supported update inbound. Themable with tmux-style config files.

%prep
%setup -q -c -T
tar -xzf %{SOURCE0}

%install
mkdir -p %{buildroot}
cp -a usr %{buildroot}/

%post
for py in /usr/bin/python3 /usr/bin/python3.11 /usr/bin/python3.12 /usr/bin/python3.13 /usr/bin/python3.14; do
    [ -x "$py" ] || continue
    "$py" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null || continue
    "$py" -I -m compileall -q -j 0 /usr/lib/uun >/dev/null 2>&1 || :
done
if [ "$1" -eq 1 ]; then
    echo "uun: to refresh EoL data daily, run as your normal user (NOT with sudo):"
    echo "       systemctl --user enable --now uun-eol-refresh.timer"
    echo "     servers without a desktop session also need (once): sudo loginctl enable-linger \$USER"
    echo "     or enable it for every user at their next login: sudo systemctl --global enable uun-eol-refresh.timer"
fi
:

%preun
if [ "$1" -eq 0 ]; then
    find /usr/lib/uun -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || :
fi
:

%files
%license /usr/share/doc/uun/LICENSE
%doc /usr/share/doc/uun/README.md
%doc /usr/share/doc/uun/SECURITY.md
/usr/bin/uun
/usr/lib/uun
/usr/lib/systemd/user/uun-eol-refresh.service
/usr/lib/systemd/user/uun-eol-refresh.timer

%changelog
* @CHANGELOG_DATE@ nc-intezra <nc-intezra@users.noreply.github.com> - @VERSION@-1
- Initial release.
