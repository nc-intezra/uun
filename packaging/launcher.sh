#!/bin/sh
# uun launcher (packaged build).
# Picks a Python >= 3.11 from fixed root-owned locations, never from $PATH
# (SECURITY.md UUN-02), and runs it isolated (-I: no cwd, PYTHONPATH or user
# site on sys.path, SECURITY.md UUN-01). Dependencies are bundled in /usr/lib/uun.
for py in /usr/bin/python3 /usr/bin/python3.14 /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11; do
    [ -x "$py" ] || continue
    if "$py" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$py" -I -c 'import sys; sys.argv[0] = "uun"; sys.path.insert(0, "/usr/lib/uun"); from uun.__main__ import main; sys.exit(main())' "$@"
    fi
done
echo "uun: requires Python 3.11 or newer (e.g. install the python3.11 or python3.12 package)" >&2
exit 1
