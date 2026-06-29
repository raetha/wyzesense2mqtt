"""
Tests for sensors.py — SensorRegistry (per-dongle), SENSOR_TYPES registry,
and timeout logic.

All SensorRegistry tests use the tmp_dongle_dir fixture which creates the
<config_dir>/dongles/DONGLE01/ directory used by SensorRegistry("DONGLE01").
"""

import os
import time

import pytest
import yaml
from conftest import TEST_DONGLE_MAC

# ---------------------------------------------------------------------------
# SENSOR_TYPES registry integrity
# ---------------------------------------------------------------------------


def test_all_sensor_types_have_required_keys():
    from sensors import SENSOR_TYPES

    required = {"model", "hw_version", "timeout_hours"}
    for name, meta in SENSOR_TYPES.items():
        missing = required - meta.keys()
        assert not missing, f"SENSOR_TYPES[{name!r}] missing keys: {missing}"


def test_binary_sensor_types_have_state_fields():
    from sensors import BINARY_SENSOR_TYPES, SENSOR_TYPES

    for name in BINARY_SENSOR_TYPES:
        meta = SENSOR_TYPES[name]
        assert "device_class" in meta, f"{name!r} in BINARY_SENSOR_TYPES but no device_class"
        assert "state_on" in meta, f"{name!r} missing state_on"
        assert "state_off" in meta, f"{name!r} missing state_off"


def test_climate_not_in_binary_sensor_types():
    from sensors import BINARY_SENSOR_TYPES

    assert "climate" not in BINARY_SENSOR_TYPES


def test_unknown_in_sensor_types_not_binary():
    from sensors import BINARY_SENSOR_TYPES, SENSOR_TYPES

    assert "unknown" in SENSOR_TYPES
    assert "unknown" not in BINARY_SENSOR_TYPES


# ---------------------------------------------------------------------------
# MAC validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mac,expected",
    [
        ("AAAAAAAA", True),
        ("12345678", True),
        ("ABCD1234", True),
        ("00000000", False),
        ("ABC", False),
        ("AAAAAAAAA", False),
        ("\x00\x00\x00\x00\x00\x00\x00\x00", False),
    ],
)
def test_is_valid_mac(mac, expected):
    from sensors import SensorRegistry

    assert SensorRegistry.is_valid_mac(mac) == expected


# ---------------------------------------------------------------------------
# timeout_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sensor_type,expected_hours",
    [
        ("motion", 8),
        ("motionv2", 4),
        ("switch", 8),
        ("switchv2", 4),
        ("leak", 4),
        ("climate", 4),
        ("unknown", 8),
    ],
)
def test_timeout_for_defaults(sensor_type, expected_hours):
    from sensors import SensorRegistry

    result = SensorRegistry.timeout_for({"sensor_type": sensor_type})
    assert result == expected_hours * 3600


def test_timeout_for_contact_sensor_type_default():
    """Contact sensor uses 8h (V1) or 4h (V2) type default; no per-sensor override."""
    from sensors import SensorRegistry

    assert SensorRegistry.timeout_for({"sensor_type": "switch"}) == 8 * 3600
    assert SensorRegistry.timeout_for({"sensor_type": "switchv2"}) == 4 * 3600


def test_timeout_for_type_default_ignores_legacy_timeout_key():
    """A 'timeout' key left in the sensor dict from a previous version is ignored."""
    from sensors import SensorRegistry

    # The legacy key must not influence the result; type default always wins
    assert SensorRegistry.timeout_for({"sensor_type": "motionv2", "timeout": 3600}) == 4 * 3600
    assert SensorRegistry.timeout_for({"sensor_type": "climate", "timeout": 900}) == 4 * 3600


def test_timeout_for_unknown_type_uses_unknown_default():
    from sensors import SensorRegistry

    result = SensorRegistry.timeout_for({"sensor_type": "totally_unknown_type"})
    assert result == 8 * 3600


# ---------------------------------------------------------------------------
# SensorRegistry — construction and dongle_mac property
# ---------------------------------------------------------------------------


def test_sensor_registry_dongle_mac(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    assert r.dongle_mac == TEST_DONGLE_MAC


def test_sensor_registry_separate_instances(tmp_config_dir):
    """Two SensorRegistry instances for different dongles are independent."""
    import config as cfg_module
    from sensors import SensorRegistry

    cfg_module.ensure_dongle_dir("DONGLE_A")
    cfg_module.ensure_dongle_dir("DONGLE_B")

    r_a = SensorRegistry("DONGLE_A")
    r_b = SensorRegistry("DONGLE_B")

    r_a.load_sensors()
    r_b.load_sensors()

    r_a.add_sensor("AAAAAAAA", "motion")
    r_b.add_sensor("BBBBBBBB", "switch")

    assert "AAAAAAAA" in r_a.sensors
    assert "AAAAAAAA" not in r_b.sensors
    assert "BBBBBBBB" in r_b.sensors
    assert "BBBBBBBB" not in r_a.sensors


# ---------------------------------------------------------------------------
# SensorRegistry — load/save sensors.yaml (per-dongle path)
# ---------------------------------------------------------------------------


def test_load_sensors_no_file(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    assert r.load_sensors() is False
    assert r.sensors == {}


def test_add_sensor_persists(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion", "19")

    r2 = SensorRegistry(TEST_DONGLE_MAC)
    r2.load_sensors()
    assert "AAAAAAAA" in r2.sensors
    assert r2.sensors["AAAAAAAA"]["sensor_type"] == "motion"
    assert r2.sensors["AAAAAAAA"]["sw_version"] == "19"


def test_add_sensor_sets_class_from_type(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "switch")
    assert r.sensors["AAAAAAAA"]["class"] == "opening"


def test_add_sensor_sets_invert_state_default(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA")
    assert r.sensors["AAAAAAAA"]["invert_state"] is False


def test_delete_sensor(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    r.add_sensor("BBBBBBBB", "switch")

    assert r.delete_sensor("AAAAAAAA") is True
    assert "AAAAAAAA" not in r.sensors
    assert "BBBBBBBB" in r.sensors

    r2 = SensorRegistry(TEST_DONGLE_MAC)
    r2.load_sensors()
    assert "AAAAAAAA" not in r2.sensors


def test_delete_sensor_also_removes_state(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    r.ensure_state_entry("AAAAAAAA")
    r.delete_sensor("AAAAAAAA")
    assert "AAAAAAAA" not in r.state


def test_delete_missing_sensor_returns_false(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    assert r.delete_sensor("ZZZZZZZZ") is False


def test_update_sensor_type(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "switch")

    assert r.update_sensor_type("AAAAAAAA", "switchv2") is True
    assert r.sensors["AAAAAAAA"]["sensor_type"] == "switchv2"


def test_update_sensor_type_noop_same_value(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    assert r.update_sensor_type("AAAAAAAA", "motion") is False


def test_update_sensor_type_missing_mac(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    assert r.update_sensor_type("ZZZZZZZZ", "motion") is False


# ---------------------------------------------------------------------------
# SensorRegistry — per-dongle file paths are correct
# ---------------------------------------------------------------------------


def test_sensors_yaml_written_to_dongle_subdir(tmp_dongle_dir):
    """sensors.yaml must be written to dongles/<mac>/ not the config root."""
    import config as cfg_module
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")

    expected = cfg_module.dongle_data_path(TEST_DONGLE_MAC, "sensors.yaml")
    assert os.path.isfile(expected)
    # Must NOT exist at config root
    root_path = cfg_module.config_path("sensors.yaml")
    assert not os.path.isfile(root_path)


def test_state_yaml_written_to_dongle_subdir(tmp_dongle_dir):
    """state.yaml must be written to dongles/<mac>/ not the config root."""
    import config as cfg_module
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.ensure_state_entry("AAAAAAAA")
    r.save_state()

    expected = cfg_module.dongle_data_path(TEST_DONGLE_MAC, "state.yaml")
    assert os.path.isfile(expected)
    root_path = cfg_module.config_path("state.yaml")
    assert not os.path.isfile(root_path)


# ---------------------------------------------------------------------------
# SensorRegistry — load/save state.yaml
# ---------------------------------------------------------------------------


def test_state_round_trip(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.ensure_state_entry("AAAAAAAA")
    r.state["AAAAAAAA"]["online"] = False
    r.save_state()

    r2 = SensorRegistry(TEST_DONGLE_MAC)
    r2.load_state()
    assert r2.state["AAAAAAAA"]["online"] is False


def test_stale_state_is_discarded(tmp_dongle_dir):
    import config as cfg_module
    from sensors import STALE_STATE_SECONDS, SensorRegistry

    path = cfg_module.dongle_data_path(TEST_DONGLE_MAC, "state.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(
            {"AAAAAAAA": {"last_seen": 0.0, "online": True}, "modified": time.time() - STALE_STATE_SECONDS - 1},
            f,
        )

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_state()
    assert r.state == {}


def test_fresh_state_is_loaded(tmp_dongle_dir):
    import config as cfg_module
    from sensors import SensorRegistry

    path = cfg_module.dongle_data_path(TEST_DONGLE_MAC, "state.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(
            {"AAAAAAAA": {"last_seen": time.time(), "online": True}, "modified": time.time()},
            f,
        )

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_state()
    assert "AAAAAAAA" in r.state
    assert r.state["AAAAAAAA"]["online"] is True


# ---------------------------------------------------------------------------
# SensorRegistry — reconcile_with_dongle / prune_state_to
# ---------------------------------------------------------------------------


def test_reconcile_adds_unknown_sensors(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.load_state()

    auto = r.reconcile_with_dongle(["AAAAAAAA", "BBBBBBBB"])
    assert set(auto) == {"AAAAAAAA", "BBBBBBBB"}
    assert "AAAAAAAA" in r.sensors
    assert "BBBBBBBB" in r.sensors


def test_reconcile_does_not_readd_known_sensors(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    r.load_state()

    auto = r.reconcile_with_dongle(["AAAAAAAA"])
    assert "AAAAAAAA" not in auto


def test_reconcile_skips_invalid_macs(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.load_state()

    auto = r.reconcile_with_dongle(["00000000", "AAAAAAAA"])
    assert "00000000" not in auto
    assert "AAAAAAAA" in auto


def test_prune_state_removes_unlinked(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.ensure_state_entry("AAAAAAAA")
    r.ensure_state_entry("BBBBBBBB")

    r.prune_state_to(["AAAAAAAA"])
    assert "BBBBBBBB" not in r.state
    assert "AAAAAAAA" in r.state


def test_ensure_all_have_state(tmp_dongle_dir):
    from sensors import SensorRegistry

    r = SensorRegistry(TEST_DONGLE_MAC)
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    r.add_sensor("BBBBBBBB", "switch")

    r.ensure_all_have_state()
    assert "AAAAAAAA" in r.state
    assert "BBBBBBBB" in r.state


# ---------------------------------------------------------------------------
# get_type_meta
# ---------------------------------------------------------------------------


def test_get_type_meta_known_type():
    from sensors import SENSOR_TYPES, SensorRegistry

    meta = SensorRegistry.get_type_meta("motion")
    assert meta == SENSOR_TYPES["motion"]


def test_get_type_meta_unknown_falls_back():
    from sensors import SENSOR_TYPES, SensorRegistry

    meta = SensorRegistry.get_type_meta("totally_unknown")
    assert meta == SENSOR_TYPES["unknown"]


def test_get_type_meta_all_known_types():
    from sensors import SENSOR_TYPES, SensorRegistry

    for sensor_type in SENSOR_TYPES:
        meta = SensorRegistry.get_type_meta(sensor_type)
        assert meta == SENSOR_TYPES[sensor_type]


def test_keypad_in_sensor_types():
    from sensors import BINARY_SENSOR_TYPES, SENSOR_TYPES

    assert "keypad" in SENSOR_TYPES
    meta = SENSOR_TYPES["keypad"]
    assert meta["hw_version"] == "V2"
    assert "timeout_hours" in meta
    assert "keypad" not in BINARY_SENSOR_TYPES


def test_chime_in_sensor_types():
    from sensors import BINARY_SENSOR_TYPES, SENSOR_TYPES

    assert "chime" in SENSOR_TYPES
    meta = SENSOR_TYPES["chime"]
    assert meta["hw_version"] == "V1"
    assert "timeout_hours" in meta
    assert "chime" not in BINARY_SENSOR_TYPES


# ---------------------------------------------------------------------------
# invert_state — field management
# ---------------------------------------------------------------------------


def test_add_sensor_sets_invert_state_false_by_default(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.add_sensor("AAAAAAAA", "switch")
    assert reg.sensors["AAAAAAAA"]["invert_state"] is False


def test_load_sensors_backfills_invert_state(tmp_dongle_dir):
    """Sensors loaded from a file without invert_state get it back-filled to False."""
    import config as cfg_module
    import yaml
    from sensors import SensorRegistry

    path = cfg_module.dongle_data_path(TEST_DONGLE_MAC, cfg_module.SENSORS_CONFIG_FILE)
    with open(path, "w") as f:
        yaml.safe_dump({"AAAAAAAA": {"name": "Test", "sensor_type": "switch"}}, f)

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.load_sensors()
    assert "invert_state" in reg.sensors["AAAAAAAA"]
    assert reg.sensors["AAAAAAAA"]["invert_state"] is False


def test_load_sensors_preserves_invert_state_true(tmp_dongle_dir):
    """A pre-existing invert_state: true in the file is preserved on load."""
    import config as cfg_module
    import yaml
    from sensors import SensorRegistry

    path = cfg_module.dongle_data_path(TEST_DONGLE_MAC, cfg_module.SENSORS_CONFIG_FILE)
    with open(path, "w") as f:
        yaml.safe_dump({"AAAAAAAA": {"name": "Test", "sensor_type": "switch", "invert_state": True}}, f)

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.load_sensors()
    assert reg.sensors["AAAAAAAA"]["invert_state"] is True


def test_load_sensors_drops_legacy_timeout_key(tmp_dongle_dir):
    """A 'timeout' key left in a sensors.yaml from a previous version is silently removed."""
    import config as cfg_module
    import yaml
    from sensors import SensorRegistry

    path = cfg_module.dongle_data_path(TEST_DONGLE_MAC, cfg_module.SENSORS_CONFIG_FILE)
    with open(path, "w") as f:
        yaml.safe_dump({"AAAAAAAA": {"name": "Test", "sensor_type": "switch", "timeout": 7200}}, f)

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.load_sensors()
    assert "timeout" not in reg.sensors["AAAAAAAA"]


# ---------------------------------------------------------------------------
# DEVICE_CLASS_OPTIONS
# ---------------------------------------------------------------------------


def test_device_class_options_exist_for_contact_types():
    from sensors import DEVICE_CLASS_OPTIONS

    assert "switch" in DEVICE_CLASS_OPTIONS
    assert "switchv2" in DEVICE_CLASS_OPTIONS
    assert "opening" in DEVICE_CLASS_OPTIONS["switch"]
    assert "door" in DEVICE_CLASS_OPTIONS["switch"]


def test_device_class_options_exist_for_motion_types():
    from sensors import DEVICE_CLASS_OPTIONS

    assert "motion" in DEVICE_CLASS_OPTIONS
    assert "motionv2" in DEVICE_CLASS_OPTIONS
    assert "motion" in DEVICE_CLASS_OPTIONS["motion"]
    assert "occupancy" in DEVICE_CLASS_OPTIONS["motion"]


def test_no_device_class_options_for_leak():
    from sensors import DEVICE_CLASS_OPTIONS

    assert "leak" not in DEVICE_CLASS_OPTIONS, "Leak device_class is fixed to moisture"


def test_no_device_class_options_for_climate():
    from sensors import DEVICE_CLASS_OPTIONS

    assert "climate" not in DEVICE_CLASS_OPTIONS


# ---------------------------------------------------------------------------
# INVERTIBLE_SENSOR_TYPES
# ---------------------------------------------------------------------------


def test_invertible_sensor_types_includes_contact_and_motion():
    from sensors import INVERTIBLE_SENSOR_TYPES

    for st in ("switch", "switchv2", "motion", "motionv2"):
        assert st in INVERTIBLE_SENSOR_TYPES, f"{st} should be invertible"


def test_invertible_sensor_types_excludes_leak_and_others():
    from sensors import INVERTIBLE_SENSOR_TYPES

    for st in ("leak", "climate", "keypad", "chime", "unknown"):
        assert st not in INVERTIBLE_SENSOR_TYPES, f"{st} should NOT be invertible"


# ---------------------------------------------------------------------------
# PIN management
# ---------------------------------------------------------------------------


def test_add_pin_adds_to_empty_list(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.add_sensor("KPADKPAD", "keypad")
    result = reg.add_pin("KPADKPAD", "1234")
    assert result is True
    assert "1234" in reg.sensors["KPADKPAD"]["pins"]


def test_add_pin_no_duplicate(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.add_sensor("KPADKPAD", "keypad")
    reg.add_pin("KPADKPAD", "1234")
    result = reg.add_pin("KPADKPAD", "1234")
    assert result is False
    assert reg.sensors["KPADKPAD"]["pins"].count("1234") == 1


def test_add_pin_multiple_pins(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.add_sensor("KPADKPAD", "keypad")
    reg.add_pin("KPADKPAD", "1234")
    reg.add_pin("KPADKPAD", "5678")
    assert reg.sensors["KPADKPAD"]["pins"] == ["1234", "5678"]


def test_add_pin_unknown_mac_returns_false(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    result = reg.add_pin("UNKNOWN1", "1234")
    assert result is False


def test_clear_pins_removes_all(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.add_sensor("KPADKPAD", "keypad")
    reg.add_pin("KPADKPAD", "1234")
    reg.add_pin("KPADKPAD", "5678")
    result = reg.clear_pins("KPADKPAD")
    assert result is True
    assert reg.sensors["KPADKPAD"]["pins"] == []


def test_clear_pins_when_empty_returns_false(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.add_sensor("KPADKPAD", "keypad")
    result = reg.clear_pins("KPADKPAD")
    assert result is False


def test_clear_pins_unknown_mac_returns_false(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    result = reg.clear_pins("UNKNOWN1")
    assert result is False


def test_pin_count_empty(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.add_sensor("KPADKPAD", "keypad")
    assert reg.pin_count("KPADKPAD") == 0


def test_pin_count_after_adds(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.add_sensor("KPADKPAD", "keypad")
    reg.add_pin("KPADKPAD", "1234")
    reg.add_pin("KPADKPAD", "5678")
    assert reg.pin_count("KPADKPAD") == 2


def test_pin_count_handles_legacy_string_pin(tmp_dongle_dir):
    """A single PIN stored as a bare string (legacy format) counts as 1."""
    import config as cfg_module
    import yaml
    from sensors import SensorRegistry

    path = cfg_module.dongle_data_path(TEST_DONGLE_MAC, cfg_module.SENSORS_CONFIG_FILE)
    with open(path, "w") as f:
        yaml.safe_dump({"KPADKPAD": {"name": "Keypad", "sensor_type": "keypad", "pins": "1234"}}, f)

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.load_sensors()
    assert reg.pin_count("KPADKPAD") == 1


def test_pin_count_unknown_mac_returns_zero(tmp_dongle_dir):
    from sensors import SensorRegistry

    reg = SensorRegistry(TEST_DONGLE_MAC)
    assert reg.pin_count("UNKNOWN1") == 0


def test_add_pin_handles_legacy_string_pin(tmp_dongle_dir):
    """add_pin promotes a bare string pin to a list before appending."""
    import config as cfg_module
    import yaml
    from sensors import SensorRegistry

    path = cfg_module.dongle_data_path(TEST_DONGLE_MAC, cfg_module.SENSORS_CONFIG_FILE)
    with open(path, "w") as f:
        yaml.safe_dump({"KPADKPAD": {"name": "Keypad", "sensor_type": "keypad", "pins": "1234"}}, f)

    reg = SensorRegistry(TEST_DONGLE_MAC)
    reg.load_sensors()
    reg.add_pin("KPADKPAD", "5678")
    pins = reg.sensors["KPADKPAD"]["pins"]
    assert isinstance(pins, list)
    assert "1234" in pins
    assert "5678" in pins
