#!/bin/bash
set -euo pipefail

readonly SERVER_ROOT="${WINDROSE_SERVER_ROOT:-/home/container}"

mkdir -p \
    "${SERVER_ROOT}/R5/Saved/.windrose-var-tmp" \
    "${SERVER_ROOT}/R5"

cd "${SERVER_ROOT}"

if [[ -n "${STARTUP:-}" ]]; then
    exec /bin/bash -lc "${STARTUP}"
fi

if [[ "$#" -gt 0 ]]; then
    exec "$@"
fi

exec /usr/local/bin/windrose-start
