#!/usr/bin/env bash
# Install both apps as systemd services, so they start on boot and restart if
# they crash. Needs sudo. Undo with ./install-services.sh --remove
#
#   ./install-services.sh
#   HOST=127.0.0.1 ./install-services.sh      # this machine only
#   ./install-services.sh --remove

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER")"
HOST="${HOST:-0.0.0.0}"
MAIN_PORT="${MAIN_PORT:-5000}"
CAL_PORT="${CAL_PORT:-5001}"
UNITS=(ltq-index ltq-calibrate)

BOLD=$'\033[1m'; OFF=$'\033[0m'; GREEN=$'\033[0;32m'

if [ "${1:-}" = "--remove" ]; then
    for unit in "${UNITS[@]}"; do
        sudo systemctl disable --now "$unit" 2>/dev/null || true
        sudo rm -f "/etc/systemd/system/$unit.service"
    done
    sudo systemctl daemon-reload
    printf '\n%sServices removed.%s\n\n' "$BOLD" "$OFF"
    exit 0
fi

[ -x .venv/bin/waitress-serve ] || { echo "Run ./setup.sh first." >&2; exit 1; }
[ -f config.json ] || { echo "Run ./setup.sh first." >&2; exit 1; }

write_unit() {
    local name="$1" description="$2" module="$3" port="$4"
    sudo tee "/etc/systemd/system/$name.service" >/dev/null <<EOF
[Unit]
Description=$description
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$HERE
ExecStart=$HERE/.venv/bin/waitress-serve --host $HOST --port $port $module:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

printf '\n%sInstalling services as user %s%s\n' "$BOLD" "$RUN_USER" "$OFF"
write_unit ltq-index     "Ladder Index - compounds"   app           "$MAIN_PORT"
write_unit ltq-calibrate "Ladder Index - calibration" calibrate_app "$CAL_PORT"

sudo systemctl daemon-reload
for unit in "${UNITS[@]}"; do
    sudo systemctl enable --now "$unit" >/dev/null 2>&1
done
sleep 3

FAILED=0
for unit in "${UNITS[@]}"; do
    if systemctl is-active --quiet "$unit"; then
        printf '    %s✓%s %s running\n' "$GREEN" "$OFF" "$unit"
    else
        printf '    \033[0;31m✗\033[0m %s did not start. Logs:\n' "$unit"
        sudo journalctl -u "$unit" -n 15 --no-pager | sed 's/^/        /'
        FAILED=1
    fi
done
[ "$FAILED" -eq 0 ] || exit 1

ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
ADDRESS="${ADDRESS:-127.0.0.1}"
[ "$HOST" = "127.0.0.1" ] && ADDRESS="127.0.0.1"

printf '\n%sBoth services are running and will start on boot.%s\n\n' "$BOLD" "$OFF"
printf '  Compounds    http://%s:%s\n' "$ADDRESS" "$MAIN_PORT"
printf '  Calibration  http://%s:%s\n\n' "$ADDRESS" "$CAL_PORT"
printf '  Status    sudo systemctl status ltq-index\n'
printf '  Logs      sudo journalctl -u ltq-index -f\n'
printf '  Restart   sudo systemctl restart ltq-index ltq-calibrate\n'
printf '  Remove    ./install-services.sh --remove\n\n'
