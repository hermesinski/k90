#!/bin/sh
# Run on the Mac that opens the login browser, not through hsh/SSH.
set -eu
umask 077
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
VENV="$HOME/.cache/k90-subscription-probe/venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$HERE/requirements.txt"
exec "$VENV/bin/python" "$HERE/probe.py" "$@"
