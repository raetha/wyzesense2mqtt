"""
Tests for config.py — configuration loading, path helpers, service identity,
per-dongle paths, legacy migration, and migration tracking.
"""

import os

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
    monkeypatch.setenv("WS2M_LOG_LEVEL", "DEBUG")

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
    assert cfg["log_level"] == "DEBUG"


def test_load_config_env_float_coercion(sample_config, tmp_config_dir, monkeypatch):
    """ENV values that parse as floats are coerced to float, not left as strings."""
    monkeypatch.setenv("WS2M_HUB_REMOTE_PAIRING_SECONDS", "30.5")

    import importlib

    import config as cfg_module

    importlib.reload(cfg_module)

    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(sample_config, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["hub_remote_pairing_seconds"] == 30.5
    assert isinstance(cfg["hub_remote_pairing_seconds"], float)


def test_load_config_env_bool_none_coercion(sample_config, tmp_config_dir, monkeypatch):
    """ENV values 'true', 'false', and 'none' are coerced to Python bool/None."""
    monkeypatch.setenv("WS2M_HASS_DISCOVERY", "false")
    monkeypatch.setenv("WS2M_MQTT_PASSWORD", "none")

    import importlib

    import config as cfg_module

    importlib.reload(cfg_module)

    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(sample_config, f)

    cfg, _ = cfg_module.load_config()
    assert cfg["hass_discovery"] is False
    assert cfg["mqtt_password"] is None


def test_load_config_unprefixed_env_ignored(sample_config, tmp_config_dir, monkeypatch):
    """Unprefixed environment variables are ignored — WS2M_ prefix is required."""
    monkeypatch.setenv("MQTT_HOST", "unprefixed.local")

    import importlib

    import config as cfg_module

    importlib.reload(cfg_module)

    cfg_module.CONFIG_DIR = str(tmp_config_dir / "config")
    cfg_path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(sample_config, f)

    cfg, _ = cfg_module.load_config()
    # Unprefixed var must not override the value from config.yaml
    assert cfg["mqtt_host"] == sample_config["mqtt_host"]


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
# Hub identity
# ---------------------------------------------------------------------------


def test_load_hub_id_generates_uuid_on_first_run(tmp_config_dir):
    import config as cfg_module

    hub_id = cfg_module.load_hub_id()
    assert hub_id is not None
    assert len(hub_id) == 36  # UUID4 string length


def test_load_hub_id_persists_to_hub_yaml(tmp_config_dir):
    import config as cfg_module

    hub_id = cfg_module.load_hub_id()
    path = cfg_module.config_path(cfg_module.HUB_FILE)
    assert os.path.isfile(path)
    data = cfg_module.read_yaml(path)
    assert data["hub_id"] == hub_id


def test_load_hub_id_stable_across_calls(tmp_config_dir):
    """Calling load_hub_id twice returns the same UUID."""
    import config as cfg_module

    first = cfg_module.load_hub_id()
    second = cfg_module.load_hub_id()
    assert first == second


def test_load_hub_id_unique_across_instances(tmp_config_dir, tmp_path):
    """Two independent config dirs produce different UUIDs."""
    import config as cfg_module

    id1 = cfg_module.load_hub_id()

    # Temporarily point at a different config dir
    original = cfg_module.CONFIG_DIR
    cfg_module.CONFIG_DIR = str(tmp_path / "other_config")
    os.makedirs(cfg_module.CONFIG_DIR)
    id2 = cfg_module.load_hub_id()
    cfg_module.CONFIG_DIR = original

    assert id1 != id2


# ---------------------------------------------------------------------------
# 3.1.0 data compatibility — flat files load as-is, no migration code
# ---------------------------------------------------------------------------


def test_310_flat_files_load_without_migration(tmp_config_dir):
    """3.1.0 sensors.yaml/state.yaml are already at the canonical flat paths;
    entries simply lack an owner and are claimed by their dongle at reconcile."""
    import config as cfg_module
    from sensors import SensorRegistry

    with open(cfg_module.config_path("sensors.yaml"), "w") as f:
        yaml.safe_dump({"AAAAAAAA": {"sensor_type": "switch", "name": "Front Door"}}, f)
    with open(cfg_module.config_path("state.yaml"), "w") as f:
        yaml.safe_dump({"AAAAAAAA": {"last_seen": 100.0, "online": True}}, f)

    r = SensorRegistry()
    r.load_sensors()
    r.load_state()
    assert r.sensors["AAAAAAAA"]["name"] == "Front Door"
    assert r.owner_of("AAAAAAAA") is None

    r.reconcile_with_dongle("DONGLE_A", ["AAAAAAAA"])
    assert r.owner_of("AAAAAAAA") == "DONGLE_A"
    assert r.sensors["AAAAAAAA"]["name"] == "Front Door"


def test_write_yaml_is_atomic(tmp_config_dir):
    """write_yaml never leaves a temp file behind and replaces atomically."""
    import config as cfg_module

    path = cfg_module.config_path("atomic.yaml")
    assert cfg_module.write_yaml(path, {"a": 1}) is True
    assert cfg_module.write_yaml(path, {"a": 2}) is True
    assert yaml.safe_load(open(path)) == {"a": 2}
    assert not os.path.exists(f"{path}.tmp")


def test_hub_ws_enabled_in_default_config():
    """hub_ws_enabled should be present in DEFAULT_CONFIG with value False."""
    import config as cfg_module

    assert "hub_ws_enabled" in cfg_module.DEFAULT_CONFIG
    assert cfg_module.DEFAULT_CONFIG["hub_ws_enabled"] is False


def test_renamed_key_usb_dongle_migrates_to_dongle(tmp_config_dir):
    """load_config migrates 3.x 'usb_dongle' key to 'dongle' transparently."""
    import os

    import config as cfg_module
    import yaml

    # Write a 3.x-style config with the old key name
    cfg_data = {
        "mqtt_host": "testbroker.local",
        "usb_dongle": "/dev/hidraw1",
    }
    cfg_path = os.path.join(cfg_module.CONFIG_DIR, cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg_data, f)

    cfg, _ = cfg_module.load_config()

    # Old key migrated to new key
    assert cfg["dongle"] == "/dev/hidraw1"
    # Old key must not appear in loaded config
    assert "usb_dongle" not in cfg


def test_renamed_key_not_written_back_to_file(tmp_config_dir):
    """After migrating usb_dongle → dongle, save_config must not write usb_dongle back."""
    import os

    import config as cfg_module
    import yaml

    cfg_data = {"mqtt_host": "testbroker.local", "usb_dongle": "/dev/hidraw2"}
    cfg_path = os.path.join(cfg_module.CONFIG_DIR, cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg_data, f)

    cfg, _ = cfg_module.load_config()
    cfg_module.save_config(cfg)

    with open(cfg_path) as f:
        saved = yaml.safe_load(f)

    assert "dongle" in saved
    assert "usb_dongle" not in saved


def test_dongle_key_in_default_config():
    """DEFAULT_CONFIG should have 'dongle', not 'usb_dongle'."""
    import config as cfg_module

    assert "dongle" in cfg_module.DEFAULT_CONFIG
    assert "usb_dongle" not in cfg_module.DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Device node resolution (_find_dev_node / list_char_devices)
# ---------------------------------------------------------------------------


def _fake_matcher_for(paths: set[str]):
    """Return a _device_matches replacement that matches exactly *paths*."""

    def _matches(path: str, major: int, minor: int) -> bool:
        return path in paths

    return _matches


def test_find_dev_node_prefers_canonical(tmp_path, monkeypatch):
    """When both the canonical node and a udev alias exist, the canonical path wins."""
    import device_discovery as cfg_module

    dev_root = tmp_path / "dev"
    (dev_root / "ws2m-dongles").mkdir(parents=True)
    canonical = dev_root / "hidraw0"
    alias = dev_root / "ws2m-dongles" / "hidraw0"
    canonical.touch()
    alias.touch()

    monkeypatch.setattr(cfg_module, "_device_matches", _fake_matcher_for({str(canonical), str(alias)}))
    result = cfg_module._find_dev_node("hidraw0", 239, 0, dev_root=str(dev_root))
    assert result == str(canonical)


def test_find_dev_node_falls_back_to_alias(tmp_path, monkeypatch):
    """Without a canonical node (container bind mount), the alias is found by scan."""
    import device_discovery as cfg_module

    dev_root = tmp_path / "dev"
    (dev_root / "ws2m-dongles").mkdir(parents=True)
    alias = dev_root / "ws2m-dongles" / "hidraw3"
    alias.touch()

    monkeypatch.setattr(cfg_module, "_device_matches", _fake_matcher_for({str(alias)}))
    result = cfg_module._find_dev_node("hidraw3", 239, 3, dev_root=str(dev_root))
    assert result == str(alias)


def test_find_dev_node_returns_none_when_absent(tmp_path, monkeypatch):
    """No matching node anywhere returns None (device not passed into container)."""
    import device_discovery as cfg_module

    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    monkeypatch.setattr(cfg_module, "_device_matches", _fake_matcher_for(set()))
    assert cfg_module._find_dev_node("hidraw5", 239, 5, dev_root=str(dev_root)) is None


def test_find_dev_node_returns_single_path(tmp_path, monkeypatch):
    """Exactly one path is returned even when several aliases match — prevents
    two DongleWorkers opening the same physical dongle."""
    import device_discovery as cfg_module

    dev_root = tmp_path / "dev"
    (dev_root / "a").mkdir(parents=True)
    (dev_root / "b").mkdir()
    alias_a = dev_root / "a" / "hidraw1"
    alias_b = dev_root / "b" / "hidraw1"
    alias_a.touch()
    alias_b.touch()

    monkeypatch.setattr(cfg_module, "_device_matches", _fake_matcher_for({str(alias_a), str(alias_b)}))
    result = cfg_module._find_dev_node("hidraw1", 239, 1, dev_root=str(dev_root))
    assert result in (str(alias_a), str(alias_b))  # one of them, never both


def test_list_char_devices_empty_or_missing_dir(tmp_path):
    import device_discovery as cfg_module

    empty = tmp_path / "empty"
    empty.mkdir()
    assert cfg_module.list_char_devices(str(empty)) == []
    assert cfg_module.list_char_devices(str(tmp_path / "does-not-exist")) == []


def test_list_char_devices_skips_regular_files(tmp_path):
    """Regular files are not character devices and must be skipped."""
    import device_discovery as cfg_module

    d = tmp_path / "dongles"
    d.mkdir()
    (d / "not-a-device").write_text("x")
    assert cfg_module.list_char_devices(str(d)) == []
