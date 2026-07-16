"""
Configuration management for WyzeSense2MQTT.

Handles loading, validating, and persisting the main config.yaml, with
automatic defaults for any missing keys and environment-variable overrides.
All file-path construction for the config directory lives here so the rest
of the codebase never hard-codes paths.
"""

import logging
import os
import uuid

import yaml

VERSION = "4.0.0"

# ---------------------------------------------------------------------------
# Directory / file name constants
# ---------------------------------------------------------------------------

# WS2M_DATA_DIR overrides the data directory at runtime.
# Defaults to "data" (relative to the working directory).
# On first start, service.sh creates a symlink /app/data → /app/config if an
# existing /app/config bind mount is detected, so Docker Compose installs
# using the old path continue to work without changes.
# The HA App sets this to "/data" via service.sh.
CONFIG_DIR = os.environ.get("WS2M_DATA_DIR", "data")

# File names (relative to CONFIG_DIR unless noted)
MAIN_CONFIG_FILE = "config.yaml"
MIGRATIONS_FILE = "migrations.yaml"
HUB_FILE = "hub.yaml"

# Sensor registry files (flat at CONFIG_DIR level; one file for all sensors
# across every dongle — dongle ownership is a per-entry key in state.yaml)
SENSORS_CONFIG_FILE = "sensors.yaml"
SENSOR_STATE_FILE = "state.yaml"


# ---------------------------------------------------------------------------
# Default configuration values
#
# These seed config.yaml on first run and fill in any keys that are absent
# from an existing file, so new settings get applied automatically on upgrade
# without requiring user action.
#
# Removed keys (kept here as comments for upgrade awareness):
#   mqtt_qos          — removed; hardcoded per-publish in mqtt.py
#   mqtt_retain       — removed; hardcoded per-publish in mqtt.py
#   publish_sensor_name — removed; sensor name is always published via device discovery
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    # MQTT broker connection
    "mqtt_host": None,
    "mqtt_port": 1883,
    "mqtt_username": None,
    "mqtt_password": None,
    "mqtt_client_id": "ws2m",
    "mqtt_clean_session": False,
    "mqtt_keepalive": 60,
    # MQTT topic root for ws2m data topics.
    # Change only when running multiple ws2m instances on the same broker.
    "self_topic_root": "ws2m",
    # Home Assistant MQTT discovery topic root.
    # HA's default is "homeassistant" and has been stable since 2021.x.
    # Only change this if you have explicitly reconfigured HA's MQTT
    # discovery prefix in configuration.yaml (mqtt: discovery_prefix: ...).
    "hass_topic_root": "homeassistant",
    # Home Assistant integration — disable to suppress all discovery publishes.
    # When set to False at startup, any previously-published discovery topics
    # are cleaned up so HA does not retain stale entities.
    # Can also be set via WS2M_HASS_DISCOVERY env var.
    "hass_discovery": True,
    # Days an orphaned sensor's configuration is retained before automatic
    # removal.  A sensor is orphaned when it is not paired to any
    # currently-connected dongle (e.g. its remote was shut down, or it was
    # unpaired outside ws2m).  Orphans keep their config and simply show as
    # unavailable in HA; if the sensor or its dongle returns within the
    # window, everything reactivates untouched.  0 disables automatic
    # removal (the "Cleanup orphaned sensors" HA button always works).
    "orphan_retention_days": 7,
    # USB dongle path:
    #   "auto"          — detect all connected WyzeSense dongles automatically
    #                     (multi-dongle supported when "auto" is used)
    #   "/dev/hidrawN"  — use exactly this one device (single-dongle)
    #   "/dev/<dir>/"   — a directory: use every character device inside it
    #                     (multi-dongle; pairs with the example udev rule that
    #                     collects all dongles under /dev/ws2m-dongles/)
    "dongle": "auto",
    # Remote bridge — WebSocket listener for remote connections.
    # Adoption is token-less: enable pairing mode from HA or via the ws2m/hub/<uuid>/remote_pair
    # MQTT topic, then start the remote. The remote auto-adopts and saves the token.
    #   hub_ws_port:       TCP port for the WebSocket listener (default 8765).
    #   hub_remote_pairing_seconds: how long pairing mode stays active after being enabled.
    #   hub_ws_mdns:       advertise the hub via mDNS/Zeroconf (default True).
    #                      Set to False if mDNS is not available or causes issues.
    #   hub_ws_enabled:    enable the WebSocket listener for remote connections (default False).
    "hub_ws_port": 8765,
    "hub_remote_pairing_seconds": 60,
    "hub_ws_mdns": True,
    "hub_ws_enabled": False,
    # Logging
    "log_level": "INFO",
}

# Keys that were present in 3.x / early 4.0 configs but are no longer used.
# Silently dropped when loading so they do not accumulate in saved config.yaml.
_REMOVED_KEYS: frozenset[str] = frozenset(["mqtt_qos", "mqtt_retain", "publish_sensor_name"])

# Keys renamed between versions; old name → new name.
# On load, the value is copied to the new key and the old key is dropped so it
# does not persist in saved config.yaml.  Handles 3.x config files gracefully.
_RENAMED_KEYS: dict[str, str] = {"usb_dongle": "dongle"}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def config_path(*parts: str) -> str:
    """Return a path rooted at CONFIG_DIR."""
    return os.path.join(CONFIG_DIR, *parts)


def read_yaml(path: str, logger: logging.Logger | None = None) -> dict | None:
    """Read and parse a YAML file. Returns None on any error."""
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except OSError as err:
        msg = f"Could not read file '{path}': {err}"
        if logger:
            logger.error(msg)
        else:
            print(msg)
        return None


def write_yaml(path: str, data: dict, logger: logging.Logger | None = None) -> bool:
    """Serialise *data* to a YAML file atomically. Returns True on success.

    The data is written to a temp file in the same directory and moved into
    place with os.replace(), so readers never observe a partially-written
    file and a crash mid-write leaves the previous file intact.
    """
    try:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w") as f:
            f.write(yaml.safe_dump(data))
        os.replace(tmp_path, path)
        return True
    except OSError as err:
        msg = f"Could not write file '{path}': {err}"
        if logger:
            logger.error(msg)
        else:
            print(msg)
        return False


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(logger: logging.Logger | None = None) -> tuple[dict | None, dict | None]:
    """Load the main configuration.

    Resolution order (later entries win):
      1. DEFAULT_CONFIG hardcoded defaults
      2. config/config.yaml (if present)
      3. WS2M_-prefixed environment variables (e.g. WS2M_MQTT_HOST).

    WS2M_DATA_DIR is handled separately at module level and is not a config key.

    Returns:
        (config, config_from_file)  –  config_from_file is None when no file
        existed yet, which callers can use to decide whether to write a new one.
        Returns (None, None) when the config is fatally invalid (e.g. no
        mqtt_host set from any source).
    """
    cfg = dict(DEFAULT_CONFIG)

    cfg_path = config_path(MAIN_CONFIG_FILE)
    config_from_file: dict | None = None

    if os.path.isfile(cfg_path):
        config_from_file = read_yaml(cfg_path, logger)
        if config_from_file:
            # Drop keys that were removed in 4.0 so they don't persist
            for key in _REMOVED_KEYS:
                config_from_file.pop(key, None)
            # Migrate renamed keys: copy value to new name, drop old name
            for old_key, new_key in _RENAMED_KEYS.items():
                if old_key in config_from_file and new_key not in config_from_file:
                    config_from_file[new_key] = config_from_file.pop(old_key)
                else:
                    config_from_file.pop(old_key, None)
            cfg.update(config_from_file)

    def _coerce(val: str):
        lower = val.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        if lower == "none":
            return None
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val)
        except ValueError:
            pass
        return val

    for env_key, env_val in os.environ.items():
        lower = env_key.lower()
        if lower.startswith("ws2m_") and lower != "ws2m_data_dir":
            key = lower[len("ws2m_") :]
            if key in cfg:
                cfg[key] = _coerce(env_val)

    # Validate required fields
    if not cfg.get("mqtt_host"):
        msg = "Configuration error: 'mqtt_host' is required but not set."
        if logger:
            logger.error(msg)
        else:
            print(msg)
        return None, None

    return cfg, config_from_file


def save_config(cfg: dict, logger: logging.Logger | None = None) -> bool:
    """Persist the current config dict to config/config.yaml.

    Removed keys are stripped before writing so they do not re-accumulate.
    """
    clean = {k: v for k, v in cfg.items() if k not in _REMOVED_KEYS and k not in _RENAMED_KEYS}
    return write_yaml(config_path(MAIN_CONFIG_FILE), clean, logger)


def init_logging(log_level: str | None = None) -> logging.Logger:
    """Configure logging to stdout and return the root ws2m logger.

    Logging goes to stdout only so Docker and systemd both capture it through
    their standard mechanisms (docker logs / journalctl) without any additional
    configuration.  The log level can be set via the 'log_level' key in
    config.yaml or the WS2M_LOG_LEVEL environment variable.
    """
    import logging.config

    level = getattr(logging, (log_level or "INFO").upper(), logging.INFO)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "verbose": {
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                    "format": "%(asctime)s %(levelname)-8s %(name)-25s %(message)s",
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "verbose",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["stdout"], "level": level},
        }
    )
    logger = logging.getLogger("ws2m")
    logger.debug("Logging initialised at level %s", (log_level or "INFO").upper())
    return logger


# ---------------------------------------------------------------------------
# Service identity
#
# A stable UUID is generated on first run and persisted to hub.yaml.
# This ensures each ws2m instance has a unique identity on the MQTT broker,
# which is important when multiple instances share the same broker.
# ---------------------------------------------------------------------------


def load_hub_id(logger: logging.Logger | None = None) -> str:
    """Return the stable hub UUID, generating and persisting it if needed."""
    path = config_path(HUB_FILE)
    if os.path.isfile(path):
        data = read_yaml(path, logger) or {}
        hub_id = data.get("hub_id")
        if hub_id:
            return str(hub_id)

    # Generate a new UUID and persist it
    hub_id = str(uuid.uuid4())
    os.makedirs(CONFIG_DIR, exist_ok=True)
    write_yaml(path, {"hub_id": hub_id}, logger)
    if logger:
        logger.info(f"Generated new hub UUID: {hub_id}")
    return hub_id


# ---------------------------------------------------------------------------
# Migration tracking
#
# Persists version markers to config/migrations.yaml so one-time startup
# tasks (discovery topic cleanup, identity renames, etc.) run exactly once
# and are skipped on subsequent restarts.
# ---------------------------------------------------------------------------

_MIGRATION_DEFAULTS: dict = {
    "discovery_schema_version": 1,
}


def load_migrations(logger: logging.Logger | None = None) -> dict:
    """Return the current migrations state dict.

    Missing keys are filled from _MIGRATION_DEFAULTS so callers can always
    rely on the full set of keys being present.
    """
    path = config_path(MIGRATIONS_FILE)
    data = {}
    if os.path.isfile(path):
        data = read_yaml(path, logger) or {}
    result = dict(_MIGRATION_DEFAULTS)
    result.update(data)
    return result


def save_migrations(state: dict, logger: logging.Logger | None = None) -> bool:
    """Persist the migrations state dict."""
    return write_yaml(config_path(MIGRATIONS_FILE), state, logger)


def get_migration_value(key: str, logger: logging.Logger | None = None):
    """Read a single migration tracking value."""
    return load_migrations(logger).get(key, _MIGRATION_DEFAULTS.get(key))


def set_migration_value(key: str, value, logger: logging.Logger | None = None) -> bool:
    """Write a single migration tracking value."""
    state = load_migrations(logger)
    state[key] = value
    return save_migrations(state, logger)
