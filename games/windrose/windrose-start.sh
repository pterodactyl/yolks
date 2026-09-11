#!/bin/bash
set -euo pipefail

readonly APPLICATION_ROOT="${WINDROSE_APPLICATION_ROOT:-/opt/windrose}"
readonly SERVER_BINARY="${APPLICATION_ROOT}/R5/Binaries/Linux/WindroseServer-Linux-Shipping"

if [[ ! -f "${SERVER_BINARY}" ]]; then
    echo "Windrose native Linux binary not found in runtime image: ${SERVER_BINARY}" >&2
    exit 1
fi

/usr/local/bin/windrose-configure
/usr/local/bin/windrose-report --watch &

cd "${APPLICATION_ROOT}"
echo "Starting the native Windrose Linux dedicated server..."
echo "Runtime image revision: ${WINDROSE_RUNTIME_REVISION:-unknown}"
echo "Official upstream image digest: ${WINDROSE_UPSTREAM_DIGEST:-unknown}"
exec "${SERVER_BINARY}" R5 -log "$@"
