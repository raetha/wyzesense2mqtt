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

import yaml

VERSION = "4.0.0"

# ---------------------------------------------------------------------------
# Directory / file name constants
# ---------------------------------------------------------------------------

CONFIG_DIR = "config"

# File names (relative to CONFIG_DIR unless noted)
MAIN_CONFIG_FILE = "config.yaml"
SENSORS_CONFIG_FILE = "sensors.yaml"
SENSOR_STATE_FILE = "state.yaml"
MIGRATIONS_FILE = "migrations.yaml"


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
    # USB dongle path ('auto' for automatic detection)
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
      3. Environment variables whose names match a config key (case-insensitive)

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

    # Environment variable overrides (same names as config keys, uppercase or lower)
    for env_key, env_val in os.environ.items():
        key = env_key.lower()
        if key not in cfg:
            continue
        # Coerce string env values to the same type as the default
        if env_val.isnumeric():
            cfg[key] = int(env_val)
        elif env_val.lower() == "true":
            cfg[key] = True
        elif env_val.lower() == "false":
            cfg[key] = False
        elif env_val.lower() == "none":
            cfg[key] = None
        else:
            cfg[key] = env_val

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
# Dongle device auto-detection
# ---------------------------------------------------------------------------


def find_dongle_device() -> str | None:
    """Scan /sys/class/hidraw for the WyzeSense bridge dongle.

    Matches USB vendor 1a86 (QinHeng Electronics) and product e024, which
    is the identifier used by the Wyze Sense Bridge HID device.
    Returns the first matching /dev/hidraw* path, or None if not found.
    """
    try:
        device_list = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode().lower()
        for line in device_list.splitlines():
            if "e024" in line and "1a86" in line:
                for part in line.split():
                    if "hidraw" in part:
                        return f"/dev/{part}"
    except (subprocess.CalledProcessError, OSError):
        pass
    return None
