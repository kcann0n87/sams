#!/usr/bin/env bash
#
# One-command start for the local web UI.
#
#   ./start.sh              open the SMS providers / Walmart stock page
#   ./start.sh sms-check    run the availability check in the terminal
#   ./start.sh <anything>   any other sams_automation subcommand
#
# Sets up a private virtualenv on first run so nothing touches your system
# Python (macOS refuses `pip install` outside a venv on recent versions).
# Playwright is NOT installed here — it's only needed for the browser
# automation, not for checking SMS providers. See README for that.

set -euo pipefail
cd "$(dirname "$0")"

# macOS has no `python`, only `python3`. Some setups have neither on PATH.
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "No Python found."
    echo "Install it from https://www.python.org/downloads/ (or: brew install python)"
    exit 1
fi

VENV=".venv"
if [ ! -d "$VENV" ]; then
    echo "First run — setting up a private Python environment (about 20 seconds)..."
    "$PY" -m venv "$VENV"
    # Only what the SMS tooling needs. Quiet, because the pip wall of text
    # buries the one line that matters if something fails.
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet PyYAML Flask
    echo "Done."
fi

export PYTHONPATH="src"

# Also make the package importable when the venv's python is run directly —
# `.venv/bin/python -m sams_automation ...` gets no PYTHONPATH from this script
# and otherwise fails with "No module named sams_automation". A .pth file in
# site-packages is read on interpreter start, so one run of start.sh fixes
# every later invocation, however it's launched.
SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
SRC="$PWD/src"
PTH="$SITE/sams_automation.pth"
if [ ! -f "$PTH" ] || [ "$(cat "$PTH" 2>/dev/null)" != "$SRC" ]; then
    printf '%s\n' "$SRC" > "$PTH"
fi

# The browser flows need Playwright and a Chromium build — a large download, so
# it's fetched on first use rather than during setup. Everything else (the SMS
# checker, the web UI) runs without it.
case "${1:-}" in
    walmart-add-phone|run|test-proxy)
        if ! "$VENV/bin/python" -c "import playwright" 2>/dev/null; then
            echo "This command drives a browser, which needs Playwright."
            echo "Installing it now — a few minutes, one time only..."
            "$VENV/bin/pip" install --quiet playwright || exit 1
            "$VENV/bin/playwright" install chromium || exit 1
            echo "Done."
        fi
        ;;
esac

if [ $# -eq 0 ]; then
    exec "$VENV/bin/python" -m sams_automation serve
fi
exec "$VENV/bin/python" -m sams_automation "$@"
