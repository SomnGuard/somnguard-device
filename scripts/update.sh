#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="somnguard-device"
APP_DIR="/opt/somnguard-device"
VENV="${APP_DIR}/.venv"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo or as root." >&2
    exit 1
fi
if [[ ! -d "$APP_DIR/.git" || ! -x "$VENV/bin/python" ]]; then
    echo "SomnGuard is not installed under $APP_DIR; run scripts/install_service.sh first." >&2
    exit 1
fi
if [[ -n "$(git -C "$APP_DIR" status --porcelain)" ]]; then
    echo "Refusing to update: $APP_DIR has local changes." >&2
    exit 1
fi
if ! git -C "$APP_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then
    echo "Current branch has no upstream; configure one before updating." >&2
    exit 1
fi

PREVIOUS_COMMIT="$(git -C "$APP_DIR" rev-parse HEAD)"
WAS_ACTIVE=0
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    WAS_ACTIVE=1
    systemctl stop "${SERVICE_NAME}.service"
fi

rollback() {
    result=$?
    trap - ERR
    if ((result != 0)); then
        echo "Update failed; restoring ${PREVIOUS_COMMIT}." >&2
        if ! git -C "$APP_DIR" reset --hard "$PREVIOUS_COMMIT"; then
            echo "Could not restore the previous source revision." >&2
        elif ! "$VENV/bin/python" -m pip install -e "$APP_DIR[full,stream]"; then
            echo "Could not restore dependencies for the previous revision." >&2
        fi
        if ((WAS_ACTIVE)); then
            if ! systemctl start "${SERVICE_NAME}.service"; then
                echo "Could not restart ${SERVICE_NAME}.service after rollback." >&2
            fi
        fi
    fi
    exit "$result"
}
trap rollback ERR

git -C "$APP_DIR" pull --ff-only
"$VENV/bin/python" -m pip install --upgrade -e "$APP_DIR[full,stream]"
"$VENV/bin/python" "$APP_DIR/scripts/download_models.py"
if ((WAS_ACTIVE)); then
    systemctl restart "${SERVICE_NAME}.service"
    systemctl is-active --quiet "${SERVICE_NAME}.service"
fi
trap - ERR
echo "SomnGuard updated from ${PREVIOUS_COMMIT} to $(git -C "$APP_DIR" rev-parse --short HEAD)."
