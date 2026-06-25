#!/bin/sh
set -e

SCRIPT_DIR=$( cd "$( dirname "$0" )" >/dev/null 2>&1 && pwd )
VIRTUAL_ENV=$SCRIPT_DIR/venv
if [ -d "$VIRTUAL_ENV" ]; then
    export VIRTUAL_ENV
    PATH="$VIRTUAL_ENV/bin:$PATH"
    export PATH
fi

# ---------------------------------------------------------------------------
# Data directory migration
#
# The default data directory was renamed from "config/" to "data/" in 4.0.
# If /app/config exists as a real directory (i.e. an existing bind mount)
# and /app/data does not yet exist, create a symlink /app/data → /app/config
# so the existing mount remains the live data directory. This runs once —
# once the symlink exists, /app/data exists and this block is skipped on all
# subsequent starts.
#
# To migrate cleanly: update your volume mount from /app/config to /app/data
# and remove the symlink. See CHANGELOG for details.
# ---------------------------------------------------------------------------

OLD_DATA="$SCRIPT_DIR/config"
NEW_DATA="$SCRIPT_DIR/data"

if [ -d "$OLD_DATA" ] && [ ! -L "$OLD_DATA" ] && [ ! -e "$NEW_DATA" ]; then
    echo "[ws2m] Detected existing data at ${OLD_DATA} — creating symlink ${NEW_DATA} → ${OLD_DATA}"
    echo "[ws2m] To migrate cleanly, update your volume mount from /app/config to /app/data and remove the symlink."
    ln -s "$OLD_DATA" "$NEW_DATA"
fi

# ---------------------------------------------------------------------------
# Home Assistant App support
#
# When running as an HA App, Supervisor writes user options to
# /data/options.json before starting the container. If that file exists,
# read it and export values as WS2M_ env vars so ws2m's config loader
# picks them up without needing a separate run script or config.yaml.
#
# For Mosquitto auto-discovery: if mqtt_host is blank, query the Supervisor
# services API (available when `services: [mqtt:want]` is in config.yaml).
# ---------------------------------------------------------------------------

OPTIONS=/data/options.json

if [ -f "$OPTIONS" ]; then
    _jq() { jq -r "$1 // empty" "$OPTIONS"; }

    MQTT_HOST=$(_jq '.mqtt_host')
    MQTT_PORT=$(_jq '.mqtt_port')

    # Auto-discover Mosquitto broker app if mqtt_host is not set
    if [ -z "$MQTT_HOST" ] && [ -n "$SUPERVISOR_TOKEN" ]; then
        echo "[ws2m] mqtt_host not set — querying Supervisor for Mosquitto..."
        MQTT_SVC=$(curl -sf \
            -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
            http://supervisor/services/mqtt 2>/dev/null || echo "")

        if echo "$MQTT_SVC" | jq -e '.data.host' > /dev/null 2>&1; then
            MQTT_HOST=$(echo "$MQTT_SVC"     | jq -r '.data.host')
            MQTT_PORT=$(echo "$MQTT_SVC"     | jq -r '.data.port // 1883')
            MQTT_USERNAME=$(echo "$MQTT_SVC" | jq -r '.data.username // empty')
            MQTT_PASSWORD=$(echo "$MQTT_SVC" | jq -r '.data.password // empty')
            echo "[ws2m] Mosquitto detected — connecting to ${MQTT_HOST}:${MQTT_PORT}"
        else
            echo "[ws2m] ERROR: mqtt_host is not set and no Mosquitto broker app was found." >&2
            exit 1
        fi
    else
        MQTT_USERNAME=$(_jq '.mqtt_username')
        MQTT_PASSWORD=$(_jq '.mqtt_password')
    fi

    # Export all options as WS2M_ env vars
    export WS2M_MQTT_HOST="${MQTT_HOST}"
    export WS2M_MQTT_PORT="${MQTT_PORT:-1883}"
    export WS2M_MQTT_USERNAME="${MQTT_USERNAME}"
    export WS2M_MQTT_PASSWORD="${MQTT_PASSWORD}"
    export WS2M_MQTT_CLIENT_ID=$(_jq '.mqtt_client_id')
    export WS2M_MQTT_CLEAN_SESSION=$(_jq '.mqtt_clean_session')
    export WS2M_MQTT_KEEPALIVE=$(_jq '.mqtt_keepalive')
    export WS2M_SELF_TOPIC_ROOT=$(_jq '.self_topic_root')
    export WS2M_HASS_TOPIC_ROOT=$(_jq '.hass_topic_root')
    export WS2M_HASS_DISCOVERY=$(_jq '.hass_discovery')
    export WS2M_USB_DONGLE=$(_jq '.usb_dongle')
    export WS2M_LOG_LEVEL=$(_jq '.log_level')

    # Point ws2m at the Supervisor persistent data volume
    export WS2M_DATA_DIR=/data
    mkdir -p "$WS2M_DATA_DIR"

    echo "[ws2m] Loaded configuration from ${OPTIONS}"
fi

cd "$SCRIPT_DIR"
python3 __main__.py "$@"
