"""
Tests for sensors.py — SensorRegistry, SENSOR_TYPES registry, and timeout logic.
"""

import os
import time

import pytest
import yaml


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


@pytest.mark.parametrize("mac,expected", [
    ("AAAAAAAA", True),
    ("12345678", True),
    ("ABCD1234", True),
    ("00000000", False),   # known-bad null MAC
    ("ABC", False),        # too short
    ("AAAAAAAAA", False),  # too long
    ("\x00\x00\x00\x00\x00\x00\x00\x00", False),
])
def test_is_valid_mac(mac, expected):
    from sensors import SensorRegistry

    assert SensorRegistry.is_valid_mac(mac) == expected


# ---------------------------------------------------------------------------
# timeout_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sensor_type,expected_hours", [
    ("motion", 8),
    ("motionv2", 4),
    ("switch", 8),
    ("switchv2", 4),
    ("leak", 4),
    ("climate", 4),
    ("unknown", 8),
])
def test_timeout_for_defaults(sensor_type, expected_hours):
    from sensors import SensorRegistry

    result = SensorRegistry.timeout_for({"sensor_type": sensor_type})
    assert result == expected_hours * 3600


def test_timeout_for_per_sensor_override_in_seconds():
    """Per-sensor 'timeout' key is in seconds, not hours."""
    from sensors import SensorRegistry

    # 7200 s = 2 h — should be returned as-is, not multiplied again
    assert SensorRegistry.timeout_for({"sensor_type": "switch", "timeout": 7200}) == 7200


def test_timeout_for_override_not_affected_by_type_default():
    """Override applies regardless of sensor type's default."""
    from sensors import SensorRegistry

    assert SensorRegistry.timeout_for({"sensor_type": "motionv2", "timeout": 3600}) == 3600
    assert SensorRegistry.timeout_for({"sensor_type": "climate", "timeout": 900}) == 900


def test_timeout_for_unknown_type_uses_unknown_default():
    from sensors import SensorRegistry

    result = SensorRegistry.timeout_for({"sensor_type": "totally_unknown_type"})
    assert result == 8 * 3600


# ---------------------------------------------------------------------------
# SensorRegistry — load/save sensors.yaml
# ---------------------------------------------------------------------------


def test_load_sensors_no_file(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    assert r.load_sensors() is False
    assert r.sensors == {}


def test_add_sensor_persists(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion", "19")

    r2 = SensorRegistry()
    r2.load_sensors()
    assert "AAAAAAAA" in r2.sensors
    assert r2.sensors["AAAAAAAA"]["sensor_type"] == "motion"
    assert r2.sensors["AAAAAAAA"]["sw_version"] == "19"


def test_add_sensor_sets_class_from_type(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "switch")
    assert r.sensors["AAAAAAAA"]["class"] == "opening"


def test_add_sensor_sets_invert_state_default(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA")
    assert r.sensors["AAAAAAAA"]["invert_state"] is False


def test_delete_sensor(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    r.add_sensor("BBBBBBBB", "switch")

    assert r.delete_sensor("AAAAAAAA") is True
    assert "AAAAAAAA" not in r.sensors
    assert "BBBBBBBB" in r.sensors

    r2 = SensorRegistry()
    r2.load_sensors()
    assert "AAAAAAAA" not in r2.sensors


def test_delete_sensor_also_removes_state(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    r.ensure_state_entry("AAAAAAAA")
    r.delete_sensor("AAAAAAAA")
    assert "AAAAAAAA" not in r.state


def test_delete_missing_sensor_returns_false(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    assert r.delete_sensor("ZZZZZZZZ") is False


def test_update_sensor_type(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "switch")

    assert r.update_sensor_type("AAAAAAAA", "switchv2") is True
    assert r.sensors["AAAAAAAA"]["sensor_type"] == "switchv2"


def test_update_sensor_type_noop_same_value(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    assert r.update_sensor_type("AAAAAAAA", "motion") is False


def test_update_sensor_type_missing_mac(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    assert r.update_sensor_type("ZZZZZZZZ", "motion") is False


# ---------------------------------------------------------------------------
# SensorRegistry — load/save state.yaml
# ---------------------------------------------------------------------------


def test_state_round_trip(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.ensure_state_entry("AAAAAAAA")
    r.state["AAAAAAAA"]["online"] = False
    r.save_state()

    r2 = SensorRegistry()
    r2.load_state()
    assert r2.state["AAAAAAAA"]["online"] is False


def test_stale_state_is_discarded(tmp_config_dir):
    import config as cfg_module
    from sensors import STALE_STATE_SECONDS, SensorRegistry

    # Write a state file with a modified timestamp well in the past
    path = os.path.join(cfg_module.CONFIG_DIR, "state.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(
            {"AAAAAAAA": {"last_seen": 0.0, "online": True}, "modified": time.time() - STALE_STATE_SECONDS - 1},
            f,
        )

    r = SensorRegistry()
    r.load_state()
    assert r.state == {}


def test_fresh_state_is_loaded(tmp_config_dir):
    import config as cfg_module
    from sensors import SensorRegistry

    path = os.path.join(cfg_module.CONFIG_DIR, "state.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(
            {"AAAAAAAA": {"last_seen": time.time(), "online": True}, "modified": time.time()},
            f,
        )

    r = SensorRegistry()
    r.load_state()
    assert "AAAAAAAA" in r.state
    assert r.state["AAAAAAAA"]["online"] is True


# ---------------------------------------------------------------------------
# SensorRegistry — reconcile_with_dongle / prune_state_to
# ---------------------------------------------------------------------------


def test_reconcile_adds_unknown_sensors(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.load_state()

    auto = r.reconcile_with_dongle(["AAAAAAAA", "BBBBBBBB"])
    assert set(auto) == {"AAAAAAAA", "BBBBBBBB"}
    assert "AAAAAAAA" in r.sensors
    assert "BBBBBBBB" in r.sensors


def test_reconcile_does_not_readd_known_sensors(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    r.load_state()

    auto = r.reconcile_with_dongle(["AAAAAAAA"])
    assert "AAAAAAAA" not in auto  # already known — not in auto_added


def test_reconcile_skips_invalid_macs(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.load_state()

    auto = r.reconcile_with_dongle(["00000000", "AAAAAAAA"])
    assert "00000000" not in auto
    assert "AAAAAAAA" in auto


def test_prune_state_removes_unlinked(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.ensure_state_entry("AAAAAAAA")
    r.ensure_state_entry("BBBBBBBB")

    r.prune_state_to(["AAAAAAAA"])
    assert "BBBBBBBB" not in r.state
    assert "AAAAAAAA" in r.state


def test_ensure_all_have_state(tmp_config_dir):
    from sensors import SensorRegistry

    r = SensorRegistry()
    r.load_sensors()
    r.add_sensor("AAAAAAAA", "motion")
    r.add_sensor("BBBBBBBB", "switch")
    # No load_state call — state starts empty

    r.ensure_all_have_state()
    assert "AAAAAAAA" in r.state
    assert "BBBBBBBB" in r.state


# ---------------------------------------------------------------------------
# get_type_meta
# ---------------------------------------------------------------------------


def test_get_type_meta_known_type():
    from sensors import SensorRegistry, SENSOR_TYPES

    meta = SensorRegistry.get_type_meta("motion")
    assert meta == SENSOR_TYPES["motion"]


def test_get_type_meta_unknown_falls_back():
    from sensors import SensorRegistry, SENSOR_TYPES

    meta = SensorRegistry.get_type_meta("totally_unknown")
    assert meta == SENSOR_TYPES["unknown"]


def test_get_type_meta_all_known_types():
    from sensors import SensorRegistry, SENSOR_TYPES

    for sensor_type in SENSOR_TYPES:
        meta = SensorRegistry.get_type_meta(sensor_type)
        assert meta == SENSOR_TYPES[sensor_type]


def test_keypad_in_sensor_types():
    """'keypad' sensor type is registered in SENSOR_TYPES."""
    from sensors import SENSOR_TYPES, BINARY_SENSOR_TYPES

    assert "keypad" in SENSOR_TYPES
    meta = SENSOR_TYPES["keypad"]
    assert meta["hw_version"] == "V2"
    assert "timeout_hours" in meta
    # Keypad is NOT a binary sensor — it has no device_class
    assert "keypad" not in BINARY_SENSOR_TYPES



def test_chime_in_sensor_types():
    """'chime' sensor type is registered in SENSOR_TYPES with correct metadata."""
    from sensors import SENSOR_TYPES, BINARY_SENSOR_TYPES

    assert "chime" in SENSOR_TYPES
    meta = SENSOR_TYPES["chime"]
    assert meta["hw_version"] == "V1"
    assert "timeout_hours" in meta
    assert "chime" not in BINARY_SENSOR_TYPES
