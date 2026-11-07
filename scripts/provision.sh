#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_USER="somnguard"
APP_DIR="/opt/somnguard-device"
DATA_DIR="/var/lib/somnguard"
ENV_DIR="/etc/somnguard"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo or as root." >&2
    exit 1
fi
if [[ ! -r /etc/os-release ]] || ! grep -Eq '^(ID|ID_LIKE)=.*(debian|raspbian)' /etc/os-release; then
    echo "SomnGuard provisioning supports Debian-based systems (including Raspberry Pi OS)." >&2
    exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
    echo "apt-get is required on this system." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends \
    ca-certificates git python3 python3-dev python3-venv python3-pip \
    build-essential pkg-config libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    libxcb1 libasound2-dev portaudio19-dev rsync

if [[ -e /etc/rpi-issue ]] || grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null; then
    apt-get install --yes --no-install-recommends python3-picamera2 python3-rpi.gpio
fi

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --create-home \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi

device_groups=()
for group in video render gpio i2c; do
    if getent group "$group" >/dev/null 2>&1; then
        device_groups+=("$group")
    fi
done
if ((${#device_groups[@]})); then
    usermod --append --groups "$(IFS=,; echo "${device_groups[*]}")" "$SERVICE_USER"
fi

install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
    "$DATA_DIR" "$DATA_DIR/db" "$DATA_DIR/media"
install -d -o root -g "$SERVICE_USER" -m 0750 "$ENV_DIR"

ENV_FILE="$ENV_DIR/device.env"
if [[ ! -e "$ENV_FILE" ]]; then
    cat >"$ENV_FILE" <<EOF
# SomnGuard production configuration. Add SOMNGUARD_PROVISION_TOKEN only when
# the device has not yet been registered. The application does not persist it.
SOMNGUARD_ENV=production
SOMNGUARD_API_URL=https://api.somnguard.tech
SOMNGUARD_DATA_DIR=$DATA_DIR
SOMNGUARD_LOG_LEVEL=INFO
EOF
fi
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

echo "Provisioning complete. Review $ENV_FILE, then run scripts/install_service.sh."
