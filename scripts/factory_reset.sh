#!/usr/bin/env bash
set -Eeuo pipefail

DATA_DIR="/var/lib/somnguard"
SERVICE_NAME="somnguard-device"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo or as root." >&2
    exit 1
fi
if [[ "${1:-}" != "--confirm" ]]; then
    echo "This removes the local device identity, buffered events, cached config, and evidence."
    echo "The .env file and installed application are preserved."
    echo "To continue, run: sudo $0 --confirm" >&2
    exit 2
fi
if [[ "$DATA_DIR" != /var/lib/somnguard && "$DATA_DIR" != /var/lib/somnguard/* ]]; then
    echo "Refusing to reset unexpected data directory: $DATA_DIR" >&2
    exit 1
fi

if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    systemctl stop "${SERVICE_NAME}.service"
fi
for path in \
    "$DATA_DIR/device_identity.json" \
    "$DATA_DIR/device_config.cache.json" \
    "$DATA_DIR/device_config.override.json" \
    "$DATA_DIR/db/somnguard_local.db" \
    "$DATA_DIR/db/somnguard_local.db-shm" \
    "$DATA_DIR/db/somnguard_local.db-wal"; do
    rm -f -- "$path"
done
if [[ -d "$DATA_DIR/media" ]]; then
    find "$DATA_DIR/media" -maxdepth 1 -type f -delete
fi
echo "Local device state removed. Restart the service to provision this device again."
