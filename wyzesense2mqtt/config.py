"""
Configuration management for WyzeSense2MQTT.

Handles loading, validating, and persisting the main config.yaml, with
automatic defaults for any missing keys and environment-variable overrides.
All file-path construction for the config directory lives here so the rest
of the codebase never hard-codes paths.
"""

import logging
import os
import subprocess
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
SERVICE_FILE = "service.yaml"

# Per-dongle data lives under <CONFIG_DIR>/dongles/<dongle_mac>/
DONGLES_DIR = "dongles"
SENSORS_CONFIG_FILE = "sensors.yaml"
SENSOR_STATE_FILE = "state.yaml"

# Legacy flat-file names (used only for migration detection)
_LEGACY_SENSORS_FILE = "sensors.yaml"
_LEGACY_STATE_FILE = "state.yaml"


# ---------------------------------------------------------------------------
# Default configuration values
#
# These seed config.yaml on first run and fill in any keys that are absent
# from an existing file, so new settings get applied automatically on upgrade
# without requiring user action.
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
    "mqtt_qos": 0,
    "mqtt_retain": True,
    # MQTT topic roots
    "self_topic_root": "ws2m",
    "hass_topic_root": "homeassistant",
    # Home Assistant integration
    "hass_discovery": True,
    # Sensor display
    "publish_sensor_name": True,
    # USB dongle path:
    #   "auto"        — detect all connected WyzeSense dongles automatically
    #                   (multi-dongle supported when "auto" is used)
    #   "/dev/hidrawN" — use exactly this one device (single-dongle)
    "usb_dongle": "auto",
    # Logging
    "log_level": "INFO",
}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def config_path(*parts: str) -> str:
    """Return a path rooted at CONFIG_DIR."""
    return os.path.join(CONFIG_DIR, *parts)


def dongle_data_path(dongle_mac: str, *parts: str) -> str:
    """Return a path rooted at <CONFIG_DIR>/dongles/<dongle_mac>/."""
    return os.path.join(CONFIG_DIR, DONGLES_DIR, dongle_mac, *parts)


def ensure_dongle_dir(dongle_mac: str) -> str:
    """Create <CONFIG_DIR>/dongles/<dongle_mac>/ if it does not exist.

    Returns the directory path.
    """
    path = dongle_data_path(dongle_mac)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Low-level YAML I/O
# ---------------------------------------------------------------------------


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
    """Serialise *data* to a YAML file. Returns True on success."""
    try:
        with open(path, "w") as f:
            f.write(yaml.safe_dump(data))
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
      3. Unprefixed environment variables matching a config key (e.g. MQTT_HOST)
         — accepted for backwards compatibility, deprecated in 4.0.
      4. WS2M_-prefixed environment variables (e.g. WS2M_MQTT_HOST) — preferred.

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
            cfg.update(config_from_file)

    def _coerce(val: str):
        if val.isnumeric():
            return int(val)
        if val.lower() == "true":
            return True
        if val.lower() == "false":
            return False
        if val.lower() == "none":
            return None
        return val

    # Collect unprefixed vars first (backwards compat), then WS2M_-prefixed
    # (preferred). Prefixed wins when both are present for the same key.
    unprefixed: dict = {}
    prefixed: dict = {}
    for env_key, env_val in os.environ.items():
        lower = env_key.lower()
        if lower.startswith("ws2m_") and lower != "ws2m_data_dir":
            key = lower[len("ws2m_") :]
            if key in cfg:
                prefixed[key] = _coerce(env_val)
        elif lower in cfg:
            unprefixed[lower] = _coerce(env_val)

    cfg.update(unprefixed)
    cfg.update(prefixed)

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
    """Persist the current config dict to config/config.yaml."""
    return write_yaml(config_path(MAIN_CONFIG_FILE), cfg, logger)


def init_logging(log_level: str | None = None) -> logging.Logger:
    """Configure logging to stdout and return the root ws2m logger.

    Logging goes to stdout only so Docker and systemd both capture it through
    their standard mechanisms (docker logs / journalctl) without any additional
    configuration.  The log level can be set via the 'log_level' key in
    config.yaml or the LOG_LEVEL environment variable.
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
# A stable UUID is generated on first run and persisted to service.yaml.
# This ensures each ws2m instance has a unique identity on the MQTT broker,
# which is important when multiple instances share the same broker.
# ---------------------------------------------------------------------------


def load_service_id(logger: logging.Logger | None = None) -> str:
    """Return the stable service UUID, generating and persisting it if needed."""
    path = config_path(SERVICE_FILE)
    if os.path.isfile(path):
        data = read_yaml(path, logger) or {}
        service_id = data.get("service_id")
        if service_id:
            return str(service_id)

    # Generate a new UUID and persist it
    service_id = str(uuid.uuid4())
    os.makedirs(CONFIG_DIR, exist_ok=True)
    write_yaml(path, {"service_id": service_id}, logger)
    if logger:
        logger.info(f"Generated new service UUID: {service_id}")
    return service_id


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


# ---------------------------------------------------------------------------
# Legacy data migration
#
# If sensors.yaml / state.yaml exist at the flat CONFIG_DIR level (pre-4.0
# single-dongle layout), migrate them into the per-dongle directory structure
# on first start.  Called by Bridge after the dongle MAC is known.
# ---------------------------------------------------------------------------


def migrate_legacy_sensor_files(dongle_mac: str, logger: logging.Logger | None = None) -> bool:
    """Move legacy flat sensors.yaml / state.yaml into dongles/<mac>/ if needed.

    Returns True if any files were migrated.
    """
    import shutil

    migrated = False
    dongle_dir = ensure_dongle_dir(dongle_mac)

    for filename in (_LEGACY_SENSORS_FILE, _LEGACY_STATE_FILE):
        legacy_path = config_path(filename)
        new_path = os.path.join(dongle_dir, filename)
        if os.path.isfile(legacy_path) and not os.path.isfile(new_path):
            shutil.move(legacy_path, new_path)
            msg = f"Migrated {legacy_path} → {new_path}"
            if logger:
                logger.info(msg)
            else:
                print(msg)
            migrated = True

    return migrated


# ---------------------------------------------------------------------------
# Dongle device auto-detection
# ---------------------------------------------------------------------------


def find_dongle_device() -> str | None:
    """Scan /sys/class/hidraw for the WyzeSense bridge dongle.

    Matches USB vendor 1a86 (QinHeng Electronics) and product e024, which
    is the identifier used by the Wyze Sense Bridge HID device.
    Returns the first matching /dev/hidraw* path, or None if not found.

    Deprecated: prefer find_all_dongle_devices() for multi-dongle support.
    Returns the first detected device for backwards compatibility.
    """
    devices = find_all_dongle_devices()
    return devices[0] if devices else None


def find_all_dongle_devices() -> list[str]:
    """Scan /sys/class/hidraw for all connected WyzeSense bridge dongles.

    Matches USB vendor 1a86 (QinHeng Electronics) and product e024.
    Returns a list of /dev/hidraw* paths (may be empty if none found).
    """
    devices: list[str] = []
    try:
        device_list = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode().lower()
        for line in device_list.splitlines():
            if "e024" in line and "1a86" in line:
                for part in line.split():
                    if "hidraw" in part:
                        devices.append(f"/dev/{part}")
                        break
    except (subprocess.CalledProcessError, OSError):
        pass
    return devices
