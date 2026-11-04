#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="somnguard-device"
SERVICE_USER="somnguard"
APP_DIR="/opt/somnguard-device"
DATA_DIR="/var/lib/somnguard"
UNIT_SOURCE="${APP_DIR}/deploy/${SERVICE_NAME}.service"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
VENV="${APP_DIR}/.venv"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo or as root." >&2
    exit 1
fi
if [[ ! -f "${APP_DIR}/pyproject.toml" || ! -f "${APP_DIR}/app/main.py" ]]; then
    echo "Application source not found under ${APP_DIR}; clone somnguard-device there first." >&2
    exit 1
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Service account '$SERVICE_USER' is missing; run scripts/provision.sh first." >&2
    exit 1
fi
if [[ ! -f "$UNIT_SOURCE" || ! -s "$UNIT_SOURCE" ]]; then
    echo "Systemd unit is missing or empty: $UNIT_SOURCE" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is required to install the service." >&2
    exit 1
fi

cd "$APP_DIR"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel
"$VENV/bin/python" -m pip install --upgrade '.[full,stream]'
"$VENV/bin/python" scripts/download_models.py

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
    "$DATA_DIR" "$DATA_DIR/db" "$DATA_DIR/media"
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"
install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_DEST"

group_dropin_dir="/etc/systemd/system/${SERVICE_NAME}.service.d"
hardware_groups=()
for group in video render gpio i2c; do
    if getent group "$group" >/dev/null 2>&1; then
        hardware_groups+=("$group")
    fi
done
if ((${#hardware_groups[@]})); then
    install -d -o root -g root -m 0755 "$group_dropin_dir"
    printf '[Service]\nSupplementaryGroups=%s\n' \
        "$(IFS=' '; echo "${hardware_groups[*]}")" \
        >"$group_dropin_dir/hardware-groups.conf"
    chown root:root "$group_dropin_dir/hardware-groups.conf"
    chmod 0644 "$group_dropin_dir/hardware-groups.conf"
elif [[ -d "$group_dropin_dir" ]]; then
    rm -f -- "$group_dropin_dir/hardware-groups.conf"
fi

systemd-analyze verify "$UNIT_DEST"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"
systemctl --no-pager --full status "${SERVICE_NAME}.service"

echo "SomnGuard device installed and running as ${SERVICE_USER}."
