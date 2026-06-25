"""
Tests for config.py — configuration loading, path helpers, service identity,
per-dongle paths, legacy migration, and migration tracking.
"""

import os

import pytest
import yaml


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_config_path_joins_correctly(tmp_config_dir):
    import config as cfg_module

    result = cfg_module.config_path("sensors.yaml")
    assert result == os.path.join(cfg_module.CONFIG_DIR, "sensors.yaml")


def test_config_path_nested(tmp_config_dir):
    import config as cfg_module

    result = cfg_module.config_path("sub", "file.yaml")
    assert result == os.path.join(cfg_module.CONFIG_DIR, "sub", "file.yaml")


def test_dongle_data_path(tmp_config_dir):
    import config as cfg_module

    result = cfg_module.dongle_data_path("AABBCCDD")
    assert result == os.path.join(cfg_module.CONFIG_DIR, "dongles", "AABBCCDD")


def test_dongle_data_path_with_file(tmp_config_dir):
    import config as cfg_module

    result = cfg_module.dongle_data_path("AABBCCDD", "sensors.yaml")
    assert result == os.path.join(cfg_module.CONFIG_DIR, "dongles", "AABBCCDD", "sensors.yaml")


def test_ensure_dongle_dir_creates_directory(tmp_config_dir):
    import config as cfg_module

    path = cfg_module.ensure_dongle_dir("AABBCCDD")
    assert os.path.isdir(path)
    assert path == cfg_module.dongle_data_path("AABBCCDD")


def test_ensure_dongle_dir_idempotent(tmp_config_dir):
    import config as cfg_module

    cfg_module.ensure_dongle_dir("AABBCCDD")
    cfg_module.ensure_dongle_dir("AABBCCDD")  # should not raise


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

    path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    data = yaml.safe_load(open(path))
    del data["mqtt_keepalive"]
    with open(path, "w") as f:
        yaml.safe_dump(data, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["mqtt_keepalive"] == cfg_module.DEFAULT_CONFIG["mqtt_keepalive"]


def test_load_config_env_override_prefixed(sample_config, tmp_config_dir, monkeypatch):
    """WS2M_-prefixed environment variables override file values (preferred form)."""
    monkeypatch.setenv("WS2M_MQTT_HOST", "envbroker.local")
    monkeypatch.setenv("WS2M_MQTT_PORT", "8883")
    monkeypatch.setenv("WS2M_MQTT_RETAIN", "false")

    import importlib
    import config as cfg_module
    importlib.reload(cfg_module)

    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(sample_config, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["mqtt_host"] == "envbroker.local"
    assert cfg["mqtt_port"] == 8883
    assert cfg["mqtt_retain"] is False


def test_load_config_env_override_unprefixed_compat(sample_config, tmp_config_dir, monkeypatch):
    """Unprefixed environment variables are still accepted (backwards compat)."""
    monkeypatch.setenv("MQTT_HOST", "envbroker.local")
    monkeypatch.setenv("MQTT_PORT", "8883")
    monkeypatch.setenv("MQTT_RETAIN", "false")

    import importlib
    import config as cfg_module
    importlib.reload(cfg_module)

    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(sample_config, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["mqtt_host"] == "envbroker.local"
    assert cfg["mqtt_port"] == 8883
    assert cfg["mqtt_retain"] is False


def test_load_config_prefixed_wins_over_unprefixed(sample_config, tmp_config_dir, monkeypatch):
    """WS2M_-prefixed vars take precedence over unprefixed vars for the same key."""
    monkeypatch.setenv("MQTT_HOST", "unprefixed.local")
    monkeypatch.setenv("WS2M_MQTT_HOST", "prefixed.local")

    import importlib
    import config as cfg_module
    importlib.reload(cfg_module)

    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(sample_config, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["mqtt_host"] == "prefixed.local"


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
# Service identity
# ---------------------------------------------------------------------------


def test_load_service_id_generates_uuid_on_first_run(tmp_config_dir):
    import config as cfg_module

    service_id = cfg_module.load_service_id()
    assert service_id is not None
    assert len(service_id) == 36  # UUID4 string length


def test_load_service_id_persists_to_service_yaml(tmp_config_dir):
    import config as cfg_module

    service_id = cfg_module.load_service_id()
    path = cfg_module.config_path(cfg_module.SERVICE_FILE)
    assert os.path.isfile(path)
    data = cfg_module.read_yaml(path)
    assert data["service_id"] == service_id


def test_load_service_id_stable_across_calls(tmp_config_dir):
    """Calling load_service_id twice returns the same UUID."""
    import config as cfg_module

    first = cfg_module.load_service_id()
    second = cfg_module.load_service_id()
    assert first == second


def test_load_service_id_unique_across_instances(tmp_config_dir, tmp_path):
    """Two independent config dirs produce different UUIDs."""
    import config as cfg_module

    id1 = cfg_module.load_service_id()

    # Temporarily point at a different config dir
    original = cfg_module.CONFIG_DIR
    cfg_module.CONFIG_DIR = str(tmp_path / "other_config")
    os.makedirs(cfg_module.CONFIG_DIR)
    id2 = cfg_module.load_service_id()
    cfg_module.CONFIG_DIR = original

    assert id1 != id2


# ---------------------------------------------------------------------------
# Legacy sensor file migration
# ---------------------------------------------------------------------------


def test_migrate_legacy_sensor_files_moves_sensors_yaml(tmp_config_dir):
    """Legacy sensors.yaml at config root is moved into dongles/<mac>/ subdir."""
    import config as cfg_module

    # Write legacy flat sensors.yaml
    legacy_path = cfg_module.config_path("sensors.yaml")
    with open(legacy_path, "w") as f:
        yaml.safe_dump({"AAAAAAAA": {"sensor_type": "switch"}}, f)

    migrated = cfg_module.migrate_legacy_sensor_files("DONGLE01")
    assert migrated is True

    new_path = cfg_module.dongle_data_path("DONGLE01", "sensors.yaml")
    assert os.path.isfile(new_path)
    assert not os.path.isfile(legacy_path)

    data = yaml.safe_load(open(new_path))
    assert "AAAAAAAA" in data


def test_migrate_legacy_sensor_files_moves_state_yaml(tmp_config_dir):
    """Legacy state.yaml at config root is moved into dongles/<mac>/ subdir."""
    import config as cfg_module

    legacy_path = cfg_module.config_path("state.yaml")
    with open(legacy_path, "w") as f:
        yaml.safe_dump({"AAAAAAAA": {"online": True}}, f)

    cfg_module.migrate_legacy_sensor_files("DONGLE01")

    new_path = cfg_module.dongle_data_path("DONGLE01", "state.yaml")
    assert os.path.isfile(new_path)
    assert not os.path.isfile(legacy_path)


def test_migrate_legacy_sensor_files_noop_when_no_legacy_files(tmp_config_dir):
    import config as cfg_module

    migrated = cfg_module.migrate_legacy_sensor_files("DONGLE01")
    assert migrated is False


def test_migrate_legacy_sensor_files_noop_when_destination_exists(tmp_config_dir):
    """Does not overwrite an existing per-dongle file."""
    import config as cfg_module

    # Existing new-layout file
    cfg_module.ensure_dongle_dir("DONGLE01")
    new_path = cfg_module.dongle_data_path("DONGLE01", "sensors.yaml")
    with open(new_path, "w") as f:
        yaml.safe_dump({"NEW": {}}, f)

    # Legacy file that should NOT overwrite
    legacy_path = cfg_module.config_path("sensors.yaml")
    with open(legacy_path, "w") as f:
        yaml.safe_dump({"OLD": {}}, f)

    cfg_module.migrate_legacy_sensor_files("DONGLE01")

    # New file untouched
    data = yaml.safe_load(open(new_path))
    assert "NEW" in data
    # Legacy file still present (was not moved)
    assert os.path.isfile(legacy_path)


# ---------------------------------------------------------------------------
# Migration tracking
# ---------------------------------------------------------------------------


def test_load_migrations_defaults_when_no_file(tmp_config_dir):
    import config as cfg_module

    state = cfg_module.load_migrations()
    assert "discovery_schema_version" in state
    assert state["discovery_schema_version"] == 1


def test_migration_round_trip(tmp_config_dir):
    import config as cfg_module

    cfg_module.save_migrations({"discovery_schema_version": 2})
    state = cfg_module.load_migrations()
    assert state["discovery_schema_version"] == 2


def test_set_get_migration_value(tmp_config_dir):
    import config as cfg_module

    cfg_module.set_migration_value("discovery_schema_version", 3)
    assert cfg_module.get_migration_value("discovery_schema_version") == 3


def test_migration_missing_key_fills_default(tmp_config_dir):
    """A migrations.yaml that lacks a key gets the default filled in on load."""
    import config as cfg_module

    cfg_module.save_migrations({})
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
    monkeypatch.setenv("WS2M_MQTT_RETAIN", "true")
    monkeypatch.setenv("WS2M_MQTT_PASSWORD", "none")

    import importlib
    import config as cfg_module
    importlib.reload(cfg_module)
    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")

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

    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump({"mqtt_port": 1883}, f)

    logger = logging.getLogger("test_no_host")
    with patch.object(logger, "error") as mock_err:
        cfg, _ = cfg_module.load_config(logger)
        assert cfg is None
        assert mock_err.called
