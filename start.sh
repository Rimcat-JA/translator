#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'Source builds require uv and Node.js 24. Windows users can use the packaged app.' >&2
  exit 1
fi
exec uv run --locked --no-dev translator start "$@"
