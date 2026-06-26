"""
Tests for bridge.py — DongleWorker event handler logic and availability checking.

DongleWorker is tested with mocked MqttGateway, SensorRegistry, and Dongle
so no real hardware or broker is required.
"""

import logging
import os
import time
from unittest.mock import MagicMock, call, patch

import pytest

from conftest import TEST_DONGLE_MAC, TEST_SERVICE_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(tmp_config_dir, sample_config):
    """Return a DongleWorker whose subsystems are all mocked."""
    from bridge import DongleWorker

    gateway = MagicMock()
    gateway.is_connected = True
    gateway.publish.return_value = MagicMock(rc=0)

    worker = DongleWorker.__new__(DongleWorker)
    worker._device_path = "/dev/hidraw0"
    worker._gateway = gateway
    worker._config = sample_config
    worker._service_id = TEST_SERVICE_ID
    worker._logger = logging.getLogger("test.worker")
    worker._initialized = True
    worker._keypad_command_topics = {}
    worker._keypad_pin_capture = {}
    worker._chime_subscribed = set()
    worker._sensor_config_subscribed = set()
    worker._auto_add_warned = set()
    worker._scan_topic = f"{sample_config['self_topic_root']}/dongle_{TEST_DONGLE_MAC}/scan"
    worker._remove_topic = f"{sample_config['self_topic_root']}/dongle_{TEST_DONGLE_MAC}/remove"
    worker._dongle_status_topic = f"{sample_config['self_topic_root']}/dongle_{TEST_DONGLE_MAC}/status"

    worker._registry = MagicMock()
    worker._registry.sensors = {}
    worker._registry.state = {}
    worker._registry.is_valid_mac = MagicMock(side_effect=lambda mac: len(mac) == 8 and mac != "00000000")

    worker._dongle = MagicMock()
    worker._dongle.mac = TEST_DONGLE_MAC

    return worker


def _make_event(
    mac="AAAAAAAA",
    event_type="alarm",
    sensor_type="switch",
    battery=80,
    signal_strength=-50,
    state="open",
    timestamp=None,
):
    """Build a synthetic SensorEvent-like object using the real SensorEvent class."""
    from dongle_protocol import SensorEvent

    return SensorEvent(
        event_type, mac, timestamp or time.time(),
        sensor_type=sensor_type, battery=battery,
        signal_strength=signal_strength, state=state,
    )


# ---------------------------------------------------------------------------
# _on_dongle_event — basic flow
# ---------------------------------------------------------------------------


def test_on_dongle_event_ignores_string_messages(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._on_dongle_event(worker._dongle, "some diagnostic string")
    worker._gateway.publish.assert_not_called()


def test_on_dongle_event_ignores_when_not_initialized(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._initialized = False
    event = _make_event()
    worker._on_dongle_event(worker._dongle, event)
    worker._gateway.publish.assert_not_called()


def test_on_dongle_event_rejects_invalid_mac(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.is_valid_mac = MagicMock(return_value=False)

    event = _make_event(mac="00000000")
    worker._on_dongle_event(worker._dongle, event)

    worker._dongle.delete.assert_called_once_with("00000000")


def test_on_dongle_event_auto_adds_new_sensor(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": True}}

    event = _make_event(mac="AAAAAAAA", sensor_type="switch")
    worker._on_dongle_event(worker._dongle, event)

    worker._registry.add_sensor.assert_called_once_with("AAAAAAAA", "switch")


def test_on_dongle_event_publishes_sensor_data(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch", "name": "Front Door"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": True}}
    worker._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA")
    worker._on_dongle_event(worker._dongle, event)

    publish_topics = [c.args[0] for c in worker._gateway.publish.call_args_list]
    assert any("wyzesense2mqtt/AAAAAAAA" == t for t in publish_topics)


def test_on_dongle_event_publishes_online_status(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": True}}
    worker._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA")
    worker._on_dongle_event(worker._dongle, event)

    publish_calls = {c.args[0]: c.args[1] for c in worker._gateway.publish.call_args_list}
    assert "wyzesense2mqtt/AAAAAAAA/status" in publish_calls
    assert publish_calls["wyzesense2mqtt/AAAAAAAA/status"] == "online"


def test_on_dongle_event_marks_offline_sensor_back_online(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": False}}
    worker._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA")
    worker._on_dongle_event(worker._dongle, event)

    assert worker._registry.state["AAAAAAAA"]["online"] is True


def test_on_dongle_event_updates_last_seen(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    old_ts = time.time() - 300
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "motion"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": old_ts, "online": True}}
    worker._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA", event_type="alarm")
    worker._on_dongle_event(worker._dongle, event)

    assert worker._registry.state["AAAAAAAA"]["last_seen"] > old_ts


def test_on_dongle_event_ignores_non_data_events(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time(), "online": True}}
    worker._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA", event_type="unknown:BB")
    worker._on_dongle_event(worker._dongle, event)

    publish_topics = [c.args[0] for c in worker._gateway.publish.call_args_list]
    assert "wyzesense2mqtt/AAAAAAAA/status" in publish_topics
    assert "wyzesense2mqtt/AAAAAAAA" not in publish_topics


def test_on_dongle_event_triggers_discovery_on_type_change(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time(), "online": True}}
    worker._registry.update_sensor_type = MagicMock(return_value=True)

    event = _make_event(mac="AAAAAAAA", sensor_type="switchv2")
    worker._on_dongle_event(worker._dongle, event)

    worker._gateway.publish_sensor_discovery.assert_called_once()


def test_on_dongle_event_payload_includes_version_fields(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch", "name": "Front Door"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time(), "online": True}}
    worker._registry.update_sensor_type = MagicMock(return_value=False)

    published_payloads = {}

    def _capture_publish(topic, payload, **kwargs):
        published_payloads[topic] = payload
        return MagicMock(rc=0)

    worker._gateway.publish = _capture_publish

    event = _make_event(mac="AAAAAAAA")
    worker._on_dongle_event(worker._dongle, event)

    assert "wyzesense2mqtt/AAAAAAAA" in published_payloads
    data = published_payloads["wyzesense2mqtt/AAAAAAAA"]
    assert "ws2m_version" in data
    assert "ws2m_discovery_schema" in data


# ---------------------------------------------------------------------------
# check_sensor_availability (formerly _check_sensor_availability)
# ---------------------------------------------------------------------------


def test_check_availability_marks_timed_out_sensor_offline(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "motion"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10 * 3600, "online": True}}
    worker._registry.timeout_for = MagicMock(return_value=8 * 3600)

    worker.check_sensor_availability()

    assert worker._registry.state["AAAAAAAA"]["online"] is False
    publish_calls = {c.args[0]: c.args[1] for c in worker._gateway.publish.call_args_list}
    assert publish_calls.get("wyzesense2mqtt/AAAAAAAA/status") == "offline"


def test_check_availability_does_not_mark_fresh_sensor_offline(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "motion"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 60, "online": True}}
    worker._registry.timeout_for = MagicMock(return_value=8 * 3600)

    worker.check_sensor_availability()

    assert worker._registry.state["AAAAAAAA"]["online"] is True
    worker._gateway.publish.assert_not_called()


def test_check_availability_skips_already_offline_sensors(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "motion"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 99999, "online": False}}
    worker._registry.timeout_for = MagicMock(return_value=8 * 3600)

    worker.check_sensor_availability()

    worker._gateway.publish.assert_not_called()


# ---------------------------------------------------------------------------
# _remove_sensor
# ---------------------------------------------------------------------------


def test_remove_sensor_calls_dongle_delete(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}

    worker._remove_sensor("AAAAAAAA")

    worker._dongle.delete.assert_called_once_with("AAAAAAAA")


def test_remove_sensor_clears_mqtt_topics(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}

    worker._remove_sensor("AAAAAAAA")

    worker._gateway.clear_sensor_topics.assert_called_once()


def test_remove_sensor_updates_registry(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}

    worker._remove_sensor("AAAAAAAA")

    worker._registry.delete_sensor.assert_called_once_with("AAAAAAAA")


def test_remove_sensor_handles_dongle_timeout(tmp_config_dir, sample_config, caplog):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    worker._dongle.delete.side_effect = TimeoutError("dongle timed out")

    with caplog.at_level(logging.ERROR):
        worker._remove_sensor("AAAAAAAA")  # should not raise

    assert any("timeout" in r.message.lower() for r in caplog.records)
    worker._registry.delete_sensor.assert_called_once_with("AAAAAAAA")


# ---------------------------------------------------------------------------
# MQTT command callbacks
# ---------------------------------------------------------------------------


def test_on_mqtt_scan_adds_sensor_on_success(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {}
    worker._dongle.scan.return_value = ("AAAAAAAA", "switch", "19")

    msg = MagicMock()
    msg.payload.decode.return_value = ""
    worker._on_mqtt_scan(None, None, msg)

    worker._registry.add_sensor.assert_called_once_with("AAAAAAAA", "switch", "19")


def test_on_mqtt_scan_no_sensor_found(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._dongle.scan.return_value = None

    msg = MagicMock()
    msg.payload.decode.return_value = ""
    worker._on_mqtt_scan(None, None, msg)

    worker._registry.add_sensor.assert_not_called()


def test_on_mqtt_scan_skips_already_known_sensor(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    worker._dongle.scan.return_value = ("AAAAAAAA", "switch", "19")

    msg = MagicMock()
    msg.payload.decode.return_value = ""
    worker._on_mqtt_scan(None, None, msg)

    worker._registry.add_sensor.assert_not_called()


def test_on_mqtt_remove(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}

    msg = MagicMock()
    msg.payload.decode.return_value = "AAAAAAAA"
    worker._on_mqtt_remove(None, None, msg)

    worker._dongle.delete.assert_called_once_with("AAAAAAAA")
    worker._registry.delete_sensor.assert_called_once_with("AAAAAAAA")


def test_on_mqtt_remove_invalid_mac(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.is_valid_mac = MagicMock(return_value=False)

    msg = MagicMock()
    msg.payload.decode.return_value = "00000000"
    worker._on_mqtt_remove(None, None, msg)

    worker._dongle.delete.assert_called_once()
    worker._registry.delete_sensor.assert_not_called()


# ---------------------------------------------------------------------------
# _mac_from_topic helper (on DongleWorker)
# ---------------------------------------------------------------------------


def _worker_for_mac_test(sample_config, root):
    from bridge import DongleWorker

    w = DongleWorker.__new__(DongleWorker)
    w._logger = logging.getLogger("test")
    w._config = {**sample_config, "self_topic_root": root}
    return w


def test_mac_from_topic_simple_root(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "ws2m")
    assert w._mac_from_topic("ws2m/AABBCCDD/set", "/set") == "AABBCCDD"


def test_mac_from_topic_slashed_root(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "home/ws2m")
    assert w._mac_from_topic("home/ws2m/AABBCCDD/set", "/set") == "AABBCCDD"


def test_mac_from_topic_wrong_prefix(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "ws2m")
    result = w._mac_from_topic("other/AABBCCDD/set", "/set")
    assert result is None


def test_mac_from_topic_wrong_suffix(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "ws2m")
    result = w._mac_from_topic("ws2m/AABBCCDD/play", "/set")
    assert result is None


def test_mac_from_topic_extra_segments(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "ws2m")
    result = w._mac_from_topic("ws2m/AABBCCDD/extra/set", "/set")
    assert result is None


# ---------------------------------------------------------------------------
# Sensor config command handlers
# ---------------------------------------------------------------------------


def test_on_mqtt_sensor_name_updates_registry_and_republishes(tmp_config_dir, sample_config):
    """_on_mqtt_sensor_name updates name, saves, and triggers re-discovery."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "name": "Old Name", "invert_state": False}
    worker._registry.state["AAAAAAAA"] = {"online": True, "last_seen": 0}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/AAAAAAAA/sensor_name/set"
    msg.payload = b"New Name"
    worker._on_mqtt_sensor_name(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["name"] == "New Name"
    worker._registry.save_sensors.assert_called()
    worker._gateway.publish_sensor_discovery.assert_called()


def test_on_mqtt_sensor_name_ignores_empty_payload(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "name": "Old Name", "invert_state": False}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/AAAAAAAA/sensor_name/set"
    msg.payload = b"   "
    worker._on_mqtt_sensor_name(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["name"] == "Old Name"


def test_on_mqtt_sensor_name_ignores_unknown_mac(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/UNKNOWN1/sensor_name/set"
    msg.payload = b"Some Name"
    worker._on_mqtt_sensor_name(None, None, msg)  # must not raise


def test_on_mqtt_device_class_updates_registry(tmp_config_dir, sample_config):
    """_on_mqtt_device_class updates class and triggers re-discovery."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "class": "opening", "invert_state": False}
    worker._registry.state["AAAAAAAA"] = {"online": True, "last_seen": 0}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/AAAAAAAA/device_class/set"
    msg.payload = b"door"
    worker._on_mqtt_device_class(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["class"] == "door"
    worker._gateway.publish_sensor_discovery.assert_called()


def test_on_mqtt_device_class_rejects_invalid_class(tmp_config_dir, sample_config):
    """_on_mqtt_device_class ignores values not in DEVICE_CLASS_OPTIONS."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "class": "opening", "invert_state": False}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/AAAAAAAA/device_class/set"
    msg.payload = b"totally_invalid"
    worker._on_mqtt_device_class(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["class"] == "opening"  # unchanged
    worker._gateway.publish_sensor_discovery.assert_not_called()


def test_on_mqtt_device_class_ignores_non_selectable_type(tmp_config_dir, sample_config):
    """_on_mqtt_device_class does nothing for sensor types without class options."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "leak"}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/AAAAAAAA/device_class/set"
    msg.payload = b"moisture"
    worker._on_mqtt_device_class(None, None, msg)  # must not raise


def test_on_mqtt_invert_state_enables_inversion(tmp_config_dir, sample_config):
    """_on_mqtt_invert_state sets invert_state True and republishes discovery."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "invert_state": False}
    worker._registry.state["AAAAAAAA"] = {"online": True, "last_seen": 0}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/AAAAAAAA/invert_state/set"
    msg.payload = b"true"
    worker._on_mqtt_invert_state(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["invert_state"] is True
    worker._gateway.publish_sensor_discovery.assert_called()


def test_on_mqtt_invert_state_disables_inversion(tmp_config_dir, sample_config):
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switchv2", "invert_state": True}
    worker._registry.state["AAAAAAAA"] = {"online": True, "last_seen": 0}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/AAAAAAAA/invert_state/set"
    msg.payload = b"false"
    worker._on_mqtt_invert_state(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["invert_state"] is False


def test_on_mqtt_invert_state_ignores_non_invertible_type(tmp_config_dir, sample_config):
    """_on_mqtt_invert_state does nothing for non-invertible sensor types."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["DDDDDDDD"] = {"sensor_type": "leak", "invert_state": False}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/DDDDDDDD/invert_state/set"
    msg.payload = b"true"
    worker._on_mqtt_invert_state(None, None, msg)

    assert worker._registry.sensors["DDDDDDDD"]["invert_state"] is False  # unchanged


def test_on_mqtt_invert_state_accepts_on_payload(tmp_config_dir, sample_config):
    """'ON' / '1' / 'yes' are all truthy payloads for invert_state."""
    from unittest.mock import MagicMock

    for payload in (b"ON", b"1", b"yes"):
        worker = _make_worker(tmp_config_dir, sample_config)
        worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "invert_state": False}
        worker._registry.state["AAAAAAAA"] = {"online": True, "last_seen": 0}
        msg = MagicMock()
        msg.topic = f"{sample_config['self_topic_root']}/AAAAAAAA/invert_state/set"
        msg.payload = payload
        worker._on_mqtt_invert_state(None, None, msg)
        assert worker._registry.sensors["AAAAAAAA"]["invert_state"] is True, f"Failed for {payload}"


# ---------------------------------------------------------------------------
# Keypad PIN management callbacks
# ---------------------------------------------------------------------------


def test_on_mqtt_keypad_add_pin_arms_capture(tmp_config_dir, sample_config):
    """add_pin button press arms the PIN capture flag for the keypad MAC."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["KPADKPAD"] = {"sensor_type": "keypad", "pins": []}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/KPADKPAD/add_pin"
    msg.payload = b"arm"
    worker._on_mqtt_keypad_add_pin(None, None, msg)

    assert worker._keypad_pin_capture.get("KPADKPAD") is True


def test_on_mqtt_keypad_add_pin_unknown_mac_no_error(tmp_config_dir, sample_config):
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/UNKNOWN1/add_pin"
    msg.payload = b"arm"
    worker._on_mqtt_keypad_add_pin(None, None, msg)  # must not raise
    assert worker._keypad_pin_capture.get("UNKNOWN1") is None


def test_on_mqtt_keypad_clear_pins_clears_registry(tmp_config_dir, sample_config):
    """clear_pins button press removes all PINs and updates pin_count state."""
    from unittest.mock import MagicMock
    from sensors import SensorRegistry

    worker = _make_worker(tmp_config_dir, sample_config)
    # Use a real SensorRegistry so clear_pins() actually works
    worker._registry = SensorRegistry(TEST_DONGLE_MAC)
    worker._registry.sensors["KPADKPAD"] = {"sensor_type": "keypad", "pins": ["1234", "5678"]}
    worker._registry.state["KPADKPAD"] = {"online": True, "last_seen": 0}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/KPADKPAD/clear_pins"
    msg.payload = b"clear"
    worker._on_mqtt_keypad_clear_pins(None, None, msg)

    assert worker._registry.sensors["KPADKPAD"]["pins"] == []
    # pin_count state should have been published
    publish_topics = [c.args[0] for c in worker._gateway.publish.call_args_list]
    assert any("/pin_count" in t for t in publish_topics)


def test_pin_capture_absorbed_on_pin_confirm_event(tmp_config_dir, sample_config):
    """When PIN capture is armed, a keypad_pin_confirm event adds the PIN."""
    from unittest.mock import MagicMock
    from sensors import SensorRegistry

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry = SensorRegistry(TEST_DONGLE_MAC)
    worker._registry.sensors["KPADKPAD"] = {"sensor_type": "keypad", "pins": []}
    worker._registry.state["KPADKPAD"] = {"online": True, "last_seen": __import__("time").time()}

    # Arm capture
    worker._keypad_pin_capture["KPADKPAD"] = True

    # Synthesise a pin_confirm event
    event = MagicMock()
    event.mac = "KPADKPAD"
    event.event = "keypad_pin_confirm"
    event.pin = "9999"
    event.timestamp = __import__("time").time()
    event.battery = 90
    event.signal_strength = -60
    event.sensor_type = "keypad"

    worker._dongle.mac = TEST_DONGLE_MAC
    worker._on_dongle_event(worker._dongle, event)

    assert "9999" in worker._registry.sensors["KPADKPAD"]["pins"]
    # Capture flag consumed
    assert worker._keypad_pin_capture.get("KPADKPAD") is None


def test_pin_capture_not_armed_does_not_add_pin(tmp_config_dir, sample_config):
    """Without arming capture, a keypad_pin_confirm event validates but does not add the PIN."""
    from unittest.mock import MagicMock
    from sensors import SensorRegistry

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry = SensorRegistry(TEST_DONGLE_MAC)
    worker._registry.sensors["KPADKPAD"] = {"sensor_type": "keypad", "pins": []}
    worker._registry.state["KPADKPAD"] = {"online": True, "last_seen": __import__("time").time()}

    event = MagicMock()
    event.mac = "KPADKPAD"
    event.event = "keypad_pin_confirm"
    event.pin = "9999"
    event.timestamp = __import__("time").time()
    event.battery = 90
    event.signal_strength = -60
    event.sensor_type = "keypad"

    worker._dongle.mac = TEST_DONGLE_MAC
    worker._on_dongle_event(worker._dongle, event)

    assert worker._registry.sensors["KPADKPAD"]["pins"] == []


# ---------------------------------------------------------------------------
# Config key removal migration
# ---------------------------------------------------------------------------


def test_removed_config_keys_not_written_by_save_config(tmp_config_dir):
    """save_config never writes removed keys even if they're present in the dict."""
    import yaml
    import config as cfg_module

    cfg = {
        "mqtt_host": "broker.local",
        "mqtt_port": 1883,
        "mqtt_username": None,
        "mqtt_password": None,
        "mqtt_client_id": "ws2m",
        "mqtt_clean_session": False,
        "mqtt_keepalive": 60,
        "self_topic_root": "ws2m",
        "hass_topic_root": "homeassistant",
        "hass_discovery": True,
        "usb_dongle": "auto",
        "log_level": "INFO",
        # Removed keys that must not appear in output:
        "mqtt_qos": 0,
        "mqtt_retain": True,
        "publish_sensor_name": True,
    }
    cfg_module.save_config(cfg)
    path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    written = yaml.safe_load(open(path))
    for key in ("mqtt_qos", "mqtt_retain", "publish_sensor_name"):
        assert key not in written, f"Removed key {key!r} was written to config"
    # hass_topic_root is NOT a removed key — it should be written
    assert "hass_topic_root" in written


def test_load_config_drops_removed_keys_from_file(tmp_config_dir, sample_config):
    """load_config strips removed keys from config.yaml so they don't re-accumulate."""
    import yaml
    import config as cfg_module

    # Inject removed keys into the file
    path = cfg_module.config_path(cfg_module.MAIN_CONFIG_FILE)
    data = yaml.safe_load(open(path)) or {}
    data["mqtt_qos"] = 0
    data["mqtt_retain"] = True
    data["publish_sensor_name"] = False
    with open(path, "w") as f:
        yaml.safe_dump(data, f)

    cfg, _ = cfg_module.load_config()
    assert cfg is not None
    for key in ("mqtt_qos", "mqtt_retain", "publish_sensor_name"):
        assert key not in cfg, f"Removed key {key!r} survived load_config"
    # hass_topic_root is NOT a removed key — it should be present in config
    assert cfg.get("hass_topic_root") == "homeassistant"


# ---------------------------------------------------------------------------
# _on_mqtt_cleanup_removed_dongles
# ---------------------------------------------------------------------------


def _make_bridge_for_cleanup(tmp_config_dir, sample_config, worker_mac=TEST_DONGLE_MAC):
    """Return a minimal Bridge instance with one mocked DongleWorker."""
    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.clear_sensor_topics = MagicMock()
    bridge._gateway.clear_dongle_all_topics = MagicMock()

    worker = MagicMock()
    worker.dongle_mac = worker_mac
    bridge._workers = [worker]
    return bridge


def test_cleanup_removed_dongles_noop_when_all_active(tmp_config_dir, sample_config):
    """Handler is a no-op when all recorded dongles match active workers."""
    import config as cfg_module

    cfg_module.ensure_dongle_dir(TEST_DONGLE_MAC)
    bridge = _make_bridge_for_cleanup(tmp_config_dir, sample_config, worker_mac=TEST_DONGLE_MAC)

    msg = MagicMock()
    bridge._on_mqtt_cleanup_removed_dongles(None, None, msg)

    bridge._gateway.clear_dongle_all_topics.assert_not_called()


def test_cleanup_removed_dongles_noop_when_no_known_macs(tmp_config_dir, sample_config):
    """Handler is a no-op when no dongle directories exist at all."""
    bridge = _make_bridge_for_cleanup(tmp_config_dir, sample_config)
    # Don't create any dongle dirs

    msg = MagicMock()
    bridge._on_mqtt_cleanup_removed_dongles(None, None, msg)

    bridge._gateway.clear_dongle_all_topics.assert_not_called()


def test_cleanup_removed_dongles_clears_missing_dongle(tmp_config_dir, sample_config):
    """Handler clears MQTT topics for a dongle that has data but no active worker."""
    import config as cfg_module

    missing_mac = "BBBBBBBB"
    cfg_module.ensure_dongle_dir(missing_mac)

    # Active worker has a different MAC — missing_mac is absent
    bridge = _make_bridge_for_cleanup(tmp_config_dir, sample_config, worker_mac=TEST_DONGLE_MAC)

    msg = MagicMock()
    bridge._on_mqtt_cleanup_removed_dongles(None, None, msg)

    bridge._gateway.clear_dongle_all_topics.assert_called_once_with(missing_mac, wait=False)


def test_cleanup_removed_dongles_deletes_data_directory(tmp_config_dir, sample_config):
    """Handler deletes the data directory for the removed dongle."""
    import config as cfg_module

    missing_mac = "BBBBBBBB"
    dongle_dir = cfg_module.ensure_dongle_dir(missing_mac)
    assert os.path.isdir(dongle_dir)

    bridge = _make_bridge_for_cleanup(tmp_config_dir, sample_config, worker_mac=TEST_DONGLE_MAC)

    msg = MagicMock()
    bridge._on_mqtt_cleanup_removed_dongles(None, None, msg)

    assert not os.path.exists(dongle_dir), "Data directory should have been deleted"


def test_cleanup_removed_dongles_clears_sensors(tmp_config_dir, sample_config):
    """Handler clears sensor topics for each sensor in the removed dongle."""
    import yaml
    import config as cfg_module

    missing_mac = "BBBBBBBB"
    sensor_mac = "CCCCCCCC"
    dongle_dir = cfg_module.ensure_dongle_dir(missing_mac)

    # Write a sensors.yaml with one sensor
    sensors_data = {sensor_mac: {"sensor_type": "switch", "sensor_name": "Test"}}
    with open(os.path.join(dongle_dir, "sensors.yaml"), "w") as f:
        yaml.safe_dump(sensors_data, f)

    bridge = _make_bridge_for_cleanup(tmp_config_dir, sample_config, worker_mac=TEST_DONGLE_MAC)

    msg = MagicMock()
    bridge._on_mqtt_cleanup_removed_dongles(None, None, msg)

    bridge._gateway.clear_sensor_topics.assert_called_once_with(sensor_mac, "switch", wait=False)
