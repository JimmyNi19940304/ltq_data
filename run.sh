#!/usr/bin/env bash
# Start both apps. Ctrl-C stops them together.
#
#   ./run.sh                 # reachable from other machines on the network
#   HOST=127.0.0.1 ./run.sh  # this machine only
#   MAIN_PORT=8000 CAL_PORT=8001 ./run.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

HOST="${HOST:-0.0.0.0}"
MAIN_PORT="${MAIN_PORT:-5000}"
CAL_PORT="${CAL_PORT:-5001}"

[ -x .venv/bin/waitress-serve ] || {
    echo "No virtual environment here yet. Run ./setup.sh first." >&2
    exit 1
}
[ -f config.json ] || {
    echo "No config.json here yet. Run ./setup.sh first." >&2
    exit 1
}

ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
ADDRESS="${ADDRESS:-127.0.0.1}"
[ "$HOST" = "127.0.0.1" ] && ADDRESS="127.0.0.1"

printf '\n\033[1mLadder Index\033[0m\n\n'
printf '  Compounds    http://%s:%s\n' "$ADDRESS" "$MAIN_PORT"
printf '  Calibration  http://%s:%s\n\n' "$ADDRESS" "$CAL_PORT"
if [ "$HOST" = "0.0.0.0" ]; then
    printf '  \033[0;33mAnyone who can reach this machine on the network can open these\n'
    printf '  pages and read files this account can read. Keep it on a trusted\n'
    printf '  network, or restrict it with a firewall.\033[0m\n\n'
fi
printf '  Ctrl-C to stop.\n\n'

.venv/bin/waitress-serve --host "$HOST" --port "$MAIN_PORT" app:app &
MAIN_PID=$!
.venv/bin/waitress-serve --host "$HOST" --port "$CAL_PORT" calibrate_app:app &
CAL_PID=$!

stop() {
    printf '\nStopping.\n'
    kill "$MAIN_PID" "$CAL_PID" 2>/dev/null || true
    wait "$MAIN_PID" "$CAL_PID" 2>/dev/null || true
    exit 0
}
trap stop INT TERM

# if either app dies on its own, take the other down too rather than half-run
wait -n "$MAIN_PID" "$CAL_PID"
printf '\n\033[0;31mOne of the apps stopped unexpectedly.\033[0m\n'
stop
