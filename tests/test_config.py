"""
Tests for config.py — configuration loading, path helpers, and migration tracking.
"""

import os

import pytest
import yaml


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_config_path_joins_correctly(tmp_config_dir, monkeypatch):
    import config as cfg_module

    result = cfg_module.config_path("sensors.yaml")
    assert result == os.path.join(cfg_module.CONFIG_DIR, "sensors.yaml")


def test_config_path_nested(tmp_config_dir, monkeypatch):
    import config as cfg_module

    result = cfg_module.config_path("sub", "file.yaml")
    assert result == os.path.join(cfg_module.CONFIG_DIR, "sub", "file.yaml")


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------


def test_read_yaml_returns_dict(tmp_config_dir):
    import config as cfg_module

    path = cfg_module.config_path("test.yaml")
    with open(path, "w") as f:
        yaml.safe_dump({"key": "value"}, f)

    result = cfg_module.read_yaml(path)
    assert result == {"key": "value"}


def test_read_yaml_missing_file_returns_none(tmp_config_dir):
    import config as cfg_module

    result = cfg_module.read_yaml(cfg_module.config_path("nonexistent.yaml"))
    assert result is None


def test_write_yaml_round_trip(tmp_config_dir):
    import config as cfg_module

    data = {"mqtt_host": "broker.local", "mqtt_port": 1883}
    path = cfg_module.config_path("roundtrip.yaml")
    assert cfg_module.write_yaml(path, data) is True
    assert cfg_module.read_yaml(path) == data


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_reads_file(sample_config, tmp_config_dir):
    import config as cfg_module

    cfg, from_file = cfg_module.load_config()
    assert cfg is not None
    assert cfg["mqtt_host"] == "testbroker.local"
    assert from_file["mqtt_host"] == "testbroker.local"


def test_load_config_fills_defaults(sample_config, tmp_config_dir):
    """Keys absent from config.yaml get filled from DEFAULT_CONFIG."""
    import config as cfg_module

    # Remove a key from the file on disk
    path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    data = yaml.safe_load(open(path))
    del data["mqtt_keepalive"]
    with open(path, "w") as f:
        yaml.safe_dump(data, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["mqtt_keepalive"] == cfg_module.DEFAULT_CONFIG["mqtt_keepalive"]


def test_load_config_env_override(sample_config, tmp_config_dir, monkeypatch):
    """Environment variables override file values."""
    monkeypatch.setenv("MQTT_HOST", "envbroker.local")
    monkeypatch.setenv("MQTT_PORT", "8883")
    monkeypatch.setenv("MQTT_RETAIN", "false")

    import importlib

    import config as cfg_module
    importlib.reload(cfg_module)  # reload to pick up fresh monkeypatched env

    # Re-write the config file after reload resets CONFIG_DIR
    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")
    import yaml
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(sample_config, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["mqtt_host"] == "envbroker.local"
    assert cfg["mqtt_port"] == 8883
    assert cfg["mqtt_retain"] is False


def test_load_config_no_mqtt_host_returns_none(tmp_config_dir):
    """load_config returns (None, None) when mqtt_host is unset."""
    import config as cfg_module

    path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(path, "w") as f:
        yaml.safe_dump({"mqtt_port": 1883}, f)

    cfg, _ = cfg_module.load_config()
    assert cfg is None


def test_load_config_no_file_no_env_returns_none(tmp_config_dir):
    import config as cfg_module

    cfg, from_file = cfg_module.load_config()
    assert cfg is None
    assert from_file is None


def test_load_config_preserves_all_user_values(sample_config, tmp_config_dir):
    """No user values are silently dropped or overwritten by defaults."""
    import config as cfg_module

    cfg, from_file = cfg_module.load_config()
    for key, value in sample_config.items():
        assert cfg[key] == value, f"Key {key!r} was altered: {cfg[key]!r} != {value!r}"


# ---------------------------------------------------------------------------
# Migration tracking
# ---------------------------------------------------------------------------


def test_load_migrations_defaults_when_no_file(tmp_config_dir):
    import config as cfg_module

    state = cfg_module.load_migrations()
    assert "discovery_schema_version" in state
    assert state["discovery_schema_version"] == 1  # default for pre-tracking installs


def test_migration_round_trip(tmp_config_dir):
    import config as cfg_module

    cfg_module.save_migrations({"discovery_schema_version": 2})
    state = cfg_module.load_migrations()
    assert state["discovery_schema_version"] == 2


def test_set_get_migration_value(tmp_config_dir):
    import config as cfg_module

    cfg_module.set_migration_value("discovery_schema_version", 2)
    assert cfg_module.get_migration_value("discovery_schema_version") == 2


def test_migration_missing_key_fills_default(tmp_config_dir):
    """A migrations.yaml that lacks a key gets the default filled in on load."""
    import config as cfg_module

    cfg_module.save_migrations({})  # empty file
    state = cfg_module.load_migrations()
    assert state["discovery_schema_version"] == 1



# ---------------------------------------------------------------------------
# write_yaml error path
# ---------------------------------------------------------------------------


def test_write_yaml_ioerror_returns_false(tmp_config_dir):
    import config as cfg_module

    result = cfg_module.write_yaml("/nonexistent/path/file.yaml", {"key": "value"})
    assert result is False


def test_write_yaml_logs_error_when_logger_provided(tmp_config_dir):
    import logging
    from unittest.mock import patch
    import config as cfg_module

    logger = logging.getLogger("test_write_error")
    with patch.object(logger, "error") as mock_err:
        cfg_module.write_yaml("/nonexistent/path/file.yaml", {}, logger)
        assert mock_err.called


def test_read_yaml_logs_error_when_logger_provided(tmp_config_dir):
    import logging
    import config as cfg_module
    from unittest.mock import patch

    logger = logging.getLogger("test")
    with patch.object(logger, "error") as mock_err:
        cfg_module.read_yaml("/nonexistent/path.yaml", logger)
        assert mock_err.called


# ---------------------------------------------------------------------------
# save_config
# ---------------------------------------------------------------------------


def test_save_config_round_trips(sample_config, tmp_config_dir):
    import config as cfg_module

    cfg_module.save_config(sample_config)
    reloaded, _ = cfg_module.load_config()
    for key, value in sample_config.items():
        assert reloaded[key] == value



def test_load_config_env_coercion_true_and_none(sample_config, tmp_config_dir, monkeypatch):
    """Env vars of 'true' and 'none' are coerced to bool True and None."""
    monkeypatch.setenv("MQTT_RETAIN", "true")
    monkeypatch.setenv("MQTT_PASSWORD", "none")

    import importlib
    import config as cfg_module
    importlib.reload(cfg_module)
    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")

    import yaml
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(sample_config, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["mqtt_retain"] is True
    assert cfg["mqtt_password"] is None


def test_load_config_logs_error_when_no_host_and_logger(tmp_config_dir):
    """load_config should call logger.error when mqtt_host is missing."""
    import logging
    from unittest.mock import patch
    import config as cfg_module

    import yaml
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump({"mqtt_port": 1883}, f)

    logger = logging.getLogger("test_no_host")
    with patch.object(logger, "error") as mock_err:
        cfg, _ = cfg_module.load_config(logger)
        assert cfg is None
        assert mock_err.called


