#!/bin/sh
# Wrapper that executes the `sieve` CLI (expensieve) via uv inside its isolated project environment
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_FLAG=""
if [ -d "$SCRIPT_DIR/.venv" ]; then
    SYNC_FLAG="--no-sync"
fi

if [ -x "/opt/homebrew/bin/uv" ]; then
    exec /opt/homebrew/bin/uv --directory "$SCRIPT_DIR" run $SYNC_FLAG sieve "$@"
elif command -v uv >/dev/null 2>&1; then
    exec uv --directory "$SCRIPT_DIR" run $SYNC_FLAG sieve "$@"
elif [ -x "$SCRIPT_DIR/.venv/bin/sieve" ]; then
    exec "$SCRIPT_DIR/.venv/bin/sieve" "$@"
else
    echo "Error: uv not found. Run 'uv sync' in $SCRIPT_DIR first." >&2
    exit 1
fi
