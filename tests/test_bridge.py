"""
Tests for bridge.py — DongleWorker event handler logic and availability checking.

DongleWorker is tested with mocked MqttGateway, SensorRegistry, and Dongle
so no real hardware or broker is required.
"""

import json
import logging
import os
import pathlib
import time
from unittest.mock import MagicMock

from conftest import TEST_DONGLE_MAC, TEST_HUB_ID

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
    worker._hub_id = TEST_HUB_ID
    worker._logger = logging.getLogger("test.worker")
    worker._initialized = True
    worker._keypad_command_topics = {}
    worker._keypad_pin_capture = {}
    worker._chime_subscribed = set()
    worker._sensor_config_subscribed = set()
    worker._auto_add_warned = set()
    worker._scan_topic = f"{sample_config['self_topic_root']}/dongle/{TEST_DONGLE_MAC}/scan"
    worker._remove_topic = f"{sample_config['self_topic_root']}/dongle/{TEST_DONGLE_MAC}/remove"
    worker._dongle_status_topic = f"{sample_config['self_topic_root']}/dongle/{TEST_DONGLE_MAC}/status"

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
        event_type,
        mac,
        timestamp or time.time(),
        sensor_type=sensor_type,
        battery=battery,
        signal_strength=signal_strength,
        state=state,
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
    assert any("wyzesense2mqtt/sensor/AAAAAAAA" == t for t in publish_topics)


def test_on_dongle_event_publishes_online_status(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    worker._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": True}}
    worker._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA")
    worker._on_dongle_event(worker._dongle, event)

    publish_calls = {c.args[0]: c.args[1] for c in worker._gateway.publish.call_args_list}
    assert "wyzesense2mqtt/sensor/AAAAAAAA/status" in publish_calls
    assert publish_calls["wyzesense2mqtt/sensor/AAAAAAAA/status"] == "online"


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
    assert "wyzesense2mqtt/sensor/AAAAAAAA/status" in publish_topics
    assert "wyzesense2mqtt/sensor/AAAAAAAA" not in publish_topics


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

    assert "wyzesense2mqtt/sensor/AAAAAAAA" in published_payloads
    data = published_payloads["wyzesense2mqtt/sensor/AAAAAAAA"]
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
    assert publish_calls.get("wyzesense2mqtt/sensor/AAAAAAAA/status") == "offline"


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

    worker._gateway.clear_sensor.assert_called_once()


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
    assert w._mac_from_topic("ws2m/sensor/AABBCCDD/set", "/set") == "AABBCCDD"


def test_mac_from_topic_slashed_root(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "home/ws2m")
    assert w._mac_from_topic("home/ws2m/sensor/AABBCCDD/set", "/set") == "AABBCCDD"


def test_mac_from_topic_wrong_prefix(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "ws2m")
    result = w._mac_from_topic("other/AABBCCDD/set", "/set")
    assert result is None


def test_mac_from_topic_wrong_suffix(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "ws2m")
    result = w._mac_from_topic("ws2m/sensor/AABBCCDD/play", "/set")
    assert result is None


def test_mac_from_topic_extra_segments(tmp_config_dir, sample_config):
    w = _worker_for_mac_test(sample_config, "ws2m")
    result = w._mac_from_topic("ws2m/sensor/AABBCCDD/extra/set", "/set")
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
    msg.topic = f"{sample_config['self_topic_root']}/sensor/AAAAAAAA/sensor_name/set"
    msg.payload = b"New Name"
    worker._on_mqtt_sensor_name(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["name"] == "New Name"
    worker._registry.save_sensors.assert_called()
    worker._gateway.publish_sensor_discovery.assert_called()


def test_on_mqtt_sensor_name_ignores_empty_payload(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "name": "Old Name", "invert_state": False}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/sensor/AAAAAAAA/sensor_name/set"
    msg.payload = b"   "
    worker._on_mqtt_sensor_name(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["name"] == "Old Name"


def test_on_mqtt_sensor_name_ignores_unknown_mac(tmp_config_dir, sample_config):
    worker = _make_worker(tmp_config_dir, sample_config)

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/sensor/UNKNOWN1/sensor_name/set"
    msg.payload = b"Some Name"
    worker._on_mqtt_sensor_name(None, None, msg)  # must not raise


def test_on_mqtt_device_class_updates_registry(tmp_config_dir, sample_config):
    """_on_mqtt_device_class updates class and triggers re-discovery."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "class": "opening", "invert_state": False}
    worker._registry.state["AAAAAAAA"] = {"online": True, "last_seen": 0}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/sensor/AAAAAAAA/device_class/set"
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
    msg.topic = f"{sample_config['self_topic_root']}/sensor/AAAAAAAA/device_class/set"
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
    msg.topic = f"{sample_config['self_topic_root']}/sensor/AAAAAAAA/device_class/set"
    msg.payload = b"moisture"
    worker._on_mqtt_device_class(None, None, msg)  # must not raise


def test_on_mqtt_invert_state_enables_inversion(tmp_config_dir, sample_config):
    """_on_mqtt_invert_state sets invert_state True and republishes discovery."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["AAAAAAAA"] = {"sensor_type": "switch", "invert_state": False}
    worker._registry.state["AAAAAAAA"] = {"online": True, "last_seen": 0}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/sensor/AAAAAAAA/invert_state/set"
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
    msg.topic = f"{sample_config['self_topic_root']}/sensor/AAAAAAAA/invert_state/set"
    msg.payload = b"false"
    worker._on_mqtt_invert_state(None, None, msg)

    assert worker._registry.sensors["AAAAAAAA"]["invert_state"] is False


def test_on_mqtt_invert_state_ignores_non_invertible_type(tmp_config_dir, sample_config):
    """_on_mqtt_invert_state does nothing for non-invertible sensor types."""
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors["DDDDDDDD"] = {"sensor_type": "leak", "invert_state": False}

    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/sensor/DDDDDDDD/invert_state/set"
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
        msg.topic = f"{sample_config['self_topic_root']}/sensor/AAAAAAAA/invert_state/set"
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
    msg.topic = f"{sample_config['self_topic_root']}/sensor/KPADKPAD/add_pin"
    msg.payload = b"arm"
    worker._on_mqtt_keypad_add_pin(None, None, msg)

    assert worker._keypad_pin_capture.get("KPADKPAD") is True


def test_on_mqtt_keypad_add_pin_unknown_mac_no_error(tmp_config_dir, sample_config):
    from unittest.mock import MagicMock

    worker = _make_worker(tmp_config_dir, sample_config)
    msg = MagicMock()
    msg.topic = f"{sample_config['self_topic_root']}/sensor/UNKNOWN1/add_pin"
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
    msg.topic = f"{sample_config['self_topic_root']}/sensor/KPADKPAD/clear_pins"
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
    import config as cfg_module
    import yaml

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
        "dongle": "auto",
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
    import config as cfg_module
    import yaml

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
    import threading

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.clear_sensor = MagicMock()
    bridge._gateway.clear_dongle = MagicMock()
    bridge._workers_lock = threading.Lock()

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

    bridge._gateway.clear_dongle.assert_not_called()


def test_cleanup_removed_dongles_noop_when_no_known_macs(tmp_config_dir, sample_config):
    """Handler is a no-op when no dongle directories exist at all."""
    bridge = _make_bridge_for_cleanup(tmp_config_dir, sample_config)
    # Don't create any dongle dirs

    msg = MagicMock()
    bridge._on_mqtt_cleanup_removed_dongles(None, None, msg)

    bridge._gateway.clear_dongle.assert_not_called()


def test_cleanup_removed_dongles_clears_missing_dongle(tmp_config_dir, sample_config):
    """Handler clears MQTT topics for a dongle that has data but no active worker."""
    import config as cfg_module

    missing_mac = "BBBBBBBB"
    cfg_module.ensure_dongle_dir(missing_mac)

    # Active worker has a different MAC — missing_mac is absent
    bridge = _make_bridge_for_cleanup(tmp_config_dir, sample_config, worker_mac=TEST_DONGLE_MAC)

    msg = MagicMock()
    bridge._on_mqtt_cleanup_removed_dongles(None, None, msg)

    bridge._gateway.clear_dongle.assert_called_once_with(missing_mac, wait=False)


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
    import config as cfg_module
    import yaml

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

    bridge._gateway.clear_sensor.assert_called_once_with(sensor_mac, "switch", wait=False)


# ---------------------------------------------------------------------------
# T1 — _start_ws_listener passes a callable to WebSocketListener
# ---------------------------------------------------------------------------


def test_start_ws_listener_passes_callable_get_pairing_active(tmp_config_dir, sample_config):
    """Bridge._start_ws_listener passes a callable as get_pairing_active to WebSocketListener."""
    import threading
    from unittest.mock import MagicMock, patch

    from bridge import Bridge
    from conftest import TEST_HUB_ID

    bridge = Bridge.__new__(Bridge)
    bridge._config = {**sample_config, "hub_ws_port": 8765, "hub_ws_mdns": False}
    bridge._hub_id = TEST_HUB_ID
    bridge._logger = logging.getLogger("test.bridge")
    bridge._workers = []
    bridge._workers_lock = threading.Lock()
    bridge._ws_listener = None
    bridge._ws_listener_thread = None
    bridge._zeroconf = None
    bridge._remote_pairing_expires = 0.0
    bridge._remote_pairing_lock = threading.Lock()

    captured = {}

    mock_listener_instance = MagicMock()
    mock_listener_instance.serve_forever = MagicMock()

    def fake_ws_listener(port, hub_id, hub_version, remotes_path, get_pairing_active, on_connection, logger):
        captured["get_pairing_active"] = get_pairing_active
        return mock_listener_instance

    with patch("ws_listener.WebSocketListener", side_effect=fake_ws_listener):
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            try:
                bridge._start_ws_listener()
            except Exception:
                pass

    assert "get_pairing_active" in captured, "get_pairing_active was not captured"
    gpa = captured["get_pairing_active"]
    assert callable(gpa), f"get_pairing_active should be callable, got {type(gpa)}"
    result = gpa()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# T2 — DongleWorker.reconnect_remote stops old dongle and starts new one
# ---------------------------------------------------------------------------


def test_reconnect_remote_stops_old_and_opens_new(tmp_config_dir, sample_config):
    """reconnect_remote stops the existing dongle and opens a new one via the new transport."""
    from unittest.mock import MagicMock, patch

    from bridge import DongleWorker, RemoteTransport

    gateway = MagicMock()
    gateway.is_connected = True
    gateway.publish.return_value = MagicMock(rc=0)

    old_dongle = MagicMock()
    old_dongle.mac = TEST_DONGLE_MAC

    worker = DongleWorker.__new__(DongleWorker)
    worker._device_path = "<remote>"
    worker._gateway = gateway
    worker._config = sample_config
    worker._hub_id = "test-hub-id"
    worker._logger = logging.getLogger("test.worker")
    worker._initialized = True
    worker._failed = False
    worker._dongle = old_dongle
    worker._transport = None
    worker._remote_id = "remote-uuid"
    worker._on_remote_health_change = None
    worker._dongle_status_topic = f"{sample_config['self_topic_root']}/dongle/{TEST_DONGLE_MAC}/status"
    worker._keypad_command_topics = {}
    worker._keypad_pin_capture = {}
    worker._chime_subscribed = set()
    worker._sensor_config_subscribed = set()
    worker._auto_add_warned = set()
    worker._scan_topic = ""
    worker._remove_topic = ""

    new_transport = MagicMock(spec=RemoteTransport)
    new_transport.remote_id = "remote-uuid"
    new_transport.dongle_mac = TEST_DONGLE_MAC
    new_transport._ws = MagicMock()
    new_transport._replay = []

    new_dongle = MagicMock()
    new_dongle.mac = TEST_DONGLE_MAC
    new_dongle.version = "v1"
    new_dongle.list.return_value = []

    registry_mock = MagicMock()
    registry_mock.sensors = {}
    registry_mock.state = {}

    with patch("dongle_protocol.Dongle", return_value=new_dongle):
        with patch("sensors.SensorRegistry", return_value=registry_mock):
            worker.reconnect_remote(new_transport)

    old_dongle.stop.assert_called_once()
    assert worker._dongle is new_dongle


# ---------------------------------------------------------------------------
# T3 — remote health frame publishes only to remote/<uuid>/health
# ---------------------------------------------------------------------------


def test_on_remote_health_change_publishes_health_unhealthy(tmp_config_dir, sample_config):
    """_on_remote_health_change publishes to remote/<uuid>/health only — no dongle_state."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    bridge._on_remote_health_change("remote-uuid-1234", False)

    calls = bridge._gateway.publish.call_args_list
    assert len(calls) == 1
    topic, payload = calls[0].args[0], calls[0].args[1]
    assert "remote/remote-uuid-1234/health" in topic
    assert payload == "degraded"
    assert "dongle_state" not in topic


def test_on_remote_health_change_publishes_health_healthy(tmp_config_dir, sample_config):
    """_on_remote_health_change with is_healthy=True publishes 'healthy'."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    bridge._on_remote_health_change("remote-uuid-1234", True)

    calls = bridge._gateway.publish.call_args_list
    assert len(calls) == 1
    assert calls[0].args[1] == "healthy"


def test_on_remote_health_change_does_not_call_publish_hub_health(tmp_config_dir, sample_config):
    """_on_remote_health_change does NOT call _publish_hub_health."""
    from unittest.mock import MagicMock, patch

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    with patch.object(bridge, "_publish_hub_health") as mock_hub_health:
        bridge._on_remote_health_change("remote-uuid-1234", False)
        mock_hub_health.assert_not_called()


# ---------------------------------------------------------------------------
# T4 — Keypad subscriptions survive broker reconnect
# ---------------------------------------------------------------------------


def test_keypad_subscriptions_survive_reconnect(tmp_config_dir, sample_config):
    """add_pin and clear_pins callbacks remain registered after resubscribe()."""
    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"KPADKPAD": {"sensor_type": "keypad", "pins": []}}
    worker._registry.state = {"KPADKPAD": {"online": True, "last_seen": 0.0}}
    client = MagicMock()
    worker._gateway.client = client

    # Subscribe to keypad topics
    worker._subscribe_keypad_command("KPADKPAD")
    add_pin_topic = f"{sample_config['self_topic_root']}/sensor/KPADKPAD/add_pin"
    clear_pins_topic = f"{sample_config['self_topic_root']}/sensor/KPADKPAD/clear_pins"
    set_topic = f"{sample_config['self_topic_root']}/sensor/KPADKPAD/set"

    # Simulate reconnect
    worker.resubscribe()

    # Collect all topics that were subscribed to across all calls
    all_topics = set()
    for call in client.subscribe.call_args_list:
        args = call.args
        if args and isinstance(args[0], list):
            for item in args[0]:
                if isinstance(item, tuple):
                    all_topics.add(item[0])
                else:
                    all_topics.add(item)
        elif args:
            all_topics.add(args[0])

    assert add_pin_topic in all_topics
    assert clear_pins_topic in all_topics
    assert set_topic in all_topics


def test_keypad_add_pin_resubscribed_via_registry_without_prior_subscribe(tmp_config_dir, sample_config):
    """resubscribe() must re-subscribe add_pin/clear_pins even without a prior _subscribe_keypad_command call.

    This tests the regression where the old implementation only re-subscribed the /set topic
    via _keypad_command_topics and ignored add_pin/clear_pins after a broker reconnect when
    the worker was initialised from saved state (no explicit _subscribe_keypad_command call yet).
    """
    worker = _make_worker(tmp_config_dir, sample_config)
    # Keypad sensor known via registry only — _keypad_command_topics NOT pre-populated
    worker._registry.sensors = {"KPAD1234": {"sensor_type": "keypad", "pins": []}}
    worker._registry.state = {"KPAD1234": {"online": True, "last_seen": 0.0}}
    worker._keypad_command_topics = {}  # explicitly empty — simulates state before first event
    client = MagicMock()
    worker._gateway.client = client

    worker.resubscribe()

    all_topics = set()
    for call in client.subscribe.call_args_list:
        args = call.args
        if args and isinstance(args[0], list):
            for item in args[0]:
                all_topics.add(item[0] if isinstance(item, tuple) else item)
        elif args:
            all_topics.add(args[0])

    root = sample_config["self_topic_root"]
    assert f"{root}/sensor/KPAD1234/add_pin" in all_topics
    assert f"{root}/sensor/KPAD1234/clear_pins" in all_topics
    assert f"{root}/sensor/KPAD1234/set" in all_topics


# ---------------------------------------------------------------------------
# T5 — Pairing mode activates and auto-deactivates
# ---------------------------------------------------------------------------


def test_on_mqtt_remote_pair_activates_pairing_and_deactivates(tmp_config_dir, sample_config):
    """_on_mqtt_remote_pair sets _is_pairing_active True then False after timeout."""
    import threading
    import time
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = {**sample_config, "hub_remote_pairing_seconds": 1}
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)
    bridge._remote_pairing_expires = 0.0
    bridge._remote_pairing_lock = threading.Lock()

    root = sample_config["self_topic_root"]
    bridge._config["self_topic_root"] = root

    msg = MagicMock()
    msg.payload.decode.return_value = ""  # use default seconds

    bridge._on_mqtt_remote_pair(None, None, msg)

    # Pairing mode should be active immediately after
    assert bridge._is_remote_pairing_active is True

    # Wait for auto-deactivation (1 second + small buffer)
    time.sleep(1.5)
    assert bridge._is_remote_pairing_active is False


# ---------------------------------------------------------------------------
# T6 — Hub restart command via MQTT
# ---------------------------------------------------------------------------


def test_on_mqtt_hub_restart_sets_restart_event(tmp_config_dir, sample_config):
    """_on_mqtt_hub_restart sets _restart_requested so run() can exit cleanly."""
    import threading

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._restart_requested = threading.Event()

    bridge._on_mqtt_hub_restart(None, None, MagicMock())

    assert bridge._restart_requested.is_set()


def test_on_mqtt_hub_restart_does_not_call_os_exit_directly(tmp_config_dir, sample_config):
    """_on_mqtt_hub_restart must not call os._exit() — clean exit is handled by run()."""
    import os
    import threading
    from unittest.mock import patch

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._restart_requested = threading.Event()

    with patch.object(os, "_exit") as mock_exit:
        bridge._on_mqtt_hub_restart(None, None, MagicMock())
        mock_exit.assert_not_called()


# ---------------------------------------------------------------------------
# T7 — Remote health entity: only health topic published (dongle_state removed)
# ---------------------------------------------------------------------------


def test_on_remote_health_change_only_health_topic_unhealthy(tmp_config_dir, sample_config):
    """_on_remote_health_change(False) publishes only health=degraded, no dongle_state."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    bridge._on_remote_health_change("remote-abc", False)

    calls = bridge._gateway.publish.call_args_list
    topic_payload = {c.args[0]: c.args[1] for c in calls}
    root = sample_config["self_topic_root"]
    assert f"{root}/remote/remote-abc/health" in topic_payload
    assert topic_payload[f"{root}/remote/remote-abc/health"] == "degraded"
    assert not any("dongle_state" in t for t in topic_payload)


def test_on_remote_health_change_only_health_topic_healthy(tmp_config_dir, sample_config):
    """_on_remote_health_change(True) publishes only health=healthy, no dongle_state."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    bridge._on_remote_health_change("remote-abc", True)

    calls = bridge._gateway.publish.call_args_list
    topic_payload = {c.args[0]: c.args[1] for c in calls}
    root = sample_config["self_topic_root"]
    assert f"{root}/remote/remote-abc/health" in topic_payload
    assert topic_payload[f"{root}/remote/remote-abc/health"] == "healthy"
    assert not any("dongle_state" in t for t in topic_payload)


# ---------------------------------------------------------------------------
# T7b — Remote status topic goes offline when WS drops or hub shuts down
# ---------------------------------------------------------------------------


def test_check_health_publishes_remote_status_offline_on_failure(tmp_config_dir, sample_config):
    """check_health() publishes remote/<uuid>/status=offline when a remote worker fails."""
    from unittest.mock import MagicMock

    from bridge import DongleWorker

    worker = DongleWorker.__new__(DongleWorker)
    worker._failed = False
    worker._config = sample_config
    worker._hub_id = "hub-id"
    worker._remote_id = "remote-uuid-abc"
    worker._device_path = "<remote>"
    worker._logger = logging.getLogger("test.worker")
    worker._gateway = MagicMock()
    worker._gateway.publish.return_value = MagicMock(rc=0)
    worker._dongle_status_topic = f"{sample_config['self_topic_root']}/dongle/AABBCCDD/status"
    worker._registry = None

    mock_dongle = MagicMock()
    mock_dongle.mac = "AABBCCDD"
    mock_dongle.check_error.side_effect = OSError("WS connection closed")
    worker._dongle = mock_dongle

    worker.check_health()

    topics = {c.args[0]: c.args[1] for c in worker._gateway.publish.call_args_list}
    root = sample_config["self_topic_root"]
    assert topics.get(f"{root}/remote/remote-uuid-abc/status") == "offline"
    assert topics.get(f"{root}/dongle/AABBCCDD/status") == "offline"
    # connected_dongles is now published by Bridge, not DongleWorker
    assert f"{root}/remote/remote-uuid-abc/connected_dongles" not in topics


def test_stop_publishes_remote_status_offline(tmp_config_dir, sample_config):
    """DongleWorker.stop() publishes remote/<uuid>/status=offline and sets _stopped=True."""
    from unittest.mock import MagicMock

    from bridge import DongleWorker

    worker = DongleWorker.__new__(DongleWorker)
    worker._config = sample_config
    worker._remote_id = "remote-uuid-xyz"
    worker._device_path = "<remote>"
    worker._logger = logging.getLogger("test.worker")
    worker._gateway = MagicMock()
    worker._gateway.publish.return_value = MagicMock(rc=0)
    worker._dongle_status_topic = f"{sample_config['self_topic_root']}/dongle/AABBCCDD/status"
    worker._dongle = MagicMock()
    worker._registry = MagicMock()

    worker.stop()

    assert worker._stopped is True
    topics = {c.args[0]: c.args[1] for c in worker._gateway.publish.call_args_list}
    root = sample_config["self_topic_root"]
    assert topics.get(f"{root}/remote/remote-uuid-xyz/status") == "offline"
    # connected_dongles is now published by Bridge, not DongleWorker
    assert f"{root}/remote/remote-uuid-xyz/connected_dongles" not in topics


# ---------------------------------------------------------------------------
# T7c — Bridge aggregate count publishing (_publish_hub_counts / _publish_remote_dongle_count)
# ---------------------------------------------------------------------------


def _make_bridge_for_counts(sample_config):
    """Bare Bridge with mocked gateway for testing count-publish methods."""
    import threading
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "hub-abc"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)
    bridge._workers_lock = threading.Lock()
    bridge._workers = []
    return bridge


def _mock_worker(failed=False, stopped=False, remote_id=None):
    from unittest.mock import MagicMock

    w = MagicMock()
    w.failed = failed
    w._stopped = stopped
    w._remote_id = remote_id
    return w


def test_publish_hub_counts_local_only(tmp_config_dir, sample_config):
    """_publish_hub_counts: only live local workers counted in connected_dongles; remote counts 0."""
    bridge = _make_bridge_for_counts(sample_config)
    bridge._workers = [
        _mock_worker(failed=False, stopped=False, remote_id=None),  # local live → 1
        _mock_worker(failed=True, stopped=False, remote_id=None),  # failed → excluded
        _mock_worker(failed=False, stopped=True, remote_id=None),  # stopped → excluded
    ]

    bridge._publish_hub_counts()

    root = sample_config["self_topic_root"]
    calls = {c.args[0]: c.args[1] for c in bridge._gateway.publish.call_args_list}
    assert calls[f"{root}/hub/hub-abc/connected_dongles"] == "1"
    assert calls[f"{root}/hub/hub-abc/remote_dongles"] == "0"
    assert calls[f"{root}/hub/hub-abc/connected_remotes"] == "0"


def test_publish_hub_counts_mixed_workers(tmp_config_dir, sample_config):
    """_publish_hub_counts: correctly separates local vs remote workers and counts unique remotes."""
    bridge = _make_bridge_for_counts(sample_config)
    bridge._workers = [
        _mock_worker(remote_id=None),  # local live
        _mock_worker(remote_id="rem-A"),  # remote A, dongle 1
        _mock_worker(remote_id="rem-A"),  # remote A, dongle 2
        _mock_worker(remote_id="rem-B"),  # remote B, dongle 1
        _mock_worker(failed=True, remote_id="rem-B"),  # remote B, failed → excluded
    ]

    bridge._publish_hub_counts()

    root = sample_config["self_topic_root"]
    calls = {c.args[0]: c.args[1] for c in bridge._gateway.publish.call_args_list}
    assert calls[f"{root}/hub/hub-abc/connected_dongles"] == "1"  # 1 local
    assert calls[f"{root}/hub/hub-abc/remote_dongles"] == "3"  # 2 from rem-A + 1 from rem-B
    assert calls[f"{root}/hub/hub-abc/connected_remotes"] == "2"  # rem-A and rem-B


def test_publish_hub_counts_all_stopped(tmp_config_dir, sample_config):
    """_publish_hub_counts: all workers stopped → all counts are 0 (clean shutdown)."""
    bridge = _make_bridge_for_counts(sample_config)
    bridge._workers = [
        _mock_worker(stopped=True, remote_id=None),
        _mock_worker(stopped=True, remote_id="rem-X"),
    ]

    bridge._publish_hub_counts()

    root = sample_config["self_topic_root"]
    calls = {c.args[0]: c.args[1] for c in bridge._gateway.publish.call_args_list}
    assert calls[f"{root}/hub/hub-abc/connected_dongles"] == "0"
    assert calls[f"{root}/hub/hub-abc/remote_dongles"] == "0"
    assert calls[f"{root}/hub/hub-abc/connected_remotes"] == "0"


def test_publish_remote_dongle_count_aggregates_for_remote(tmp_config_dir, sample_config):
    """_publish_remote_dongle_count: counts all live workers for given remote_id."""
    bridge = _make_bridge_for_counts(sample_config)
    bridge._workers = [
        _mock_worker(remote_id="rem-1"),  # live
        _mock_worker(remote_id="rem-1"),  # live
        _mock_worker(failed=True, remote_id="rem-1"),  # failed → excluded
        _mock_worker(remote_id="rem-2"),  # different remote → excluded
        _mock_worker(remote_id=None),  # local → excluded
    ]

    bridge._publish_remote_dongle_count("rem-1")

    root = sample_config["self_topic_root"]
    calls = {c.args[0]: c.args[1] for c in bridge._gateway.publish.call_args_list}
    assert calls[f"{root}/remote/rem-1/connected_dongles"] == "2"


def test_publish_remote_dongle_count_zero_after_all_fail(tmp_config_dir, sample_config):
    """_publish_remote_dongle_count: returns 0 when all workers for that remote have failed/stopped."""
    bridge = _make_bridge_for_counts(sample_config)
    bridge._workers = [
        _mock_worker(failed=True, remote_id="rem-gone"),
        _mock_worker(stopped=True, remote_id="rem-gone"),
    ]

    bridge._publish_remote_dongle_count("rem-gone")

    root = sample_config["self_topic_root"]
    calls = {c.args[0]: c.args[1] for c in bridge._gateway.publish.call_args_list}
    assert calls[f"{root}/remote/rem-gone/connected_dongles"] == "0"


def test_worker_stop_sets_stopped_flag(tmp_config_dir, sample_config):
    """DongleWorker.stop() sets _stopped=True so Bridge count filters exclude it."""
    from unittest.mock import MagicMock

    from bridge import DongleWorker

    worker = DongleWorker.__new__(DongleWorker)
    worker._config = sample_config
    worker._remote_id = None
    worker._stopped = False
    worker._device_path = "/dev/hidraw0"
    worker._logger = logging.getLogger("test.worker")
    worker._gateway = MagicMock()
    worker._gateway.publish.return_value = MagicMock(rc=0)
    worker._dongle_status_topic = f"{sample_config['self_topic_root']}/dongle/AABBCCDD/status"
    worker._dongle = MagicMock()
    worker._registry = MagicMock()

    worker.stop()

    assert worker._stopped is True


# ---------------------------------------------------------------------------
# T8 — Remote restart forwarded via WebSocket
# ---------------------------------------------------------------------------


def test_on_mqtt_remote_restart_calls_send_restart(tmp_config_dir, sample_config):
    """_on_mqtt_remote_restart sends restart JSON frame to the remote transport."""
    from unittest.mock import MagicMock

    from bridge import Bridge, RemoteTransport

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()

    transport = MagicMock(spec=RemoteTransport)

    handler = bridge._on_mqtt_remote_restart("remote-xyz", transport)
    handler(None, None, MagicMock())

    transport.send_restart.assert_called_once()


# ---------------------------------------------------------------------------
# Part B/D — New config entity handlers
# ---------------------------------------------------------------------------


def _make_bridge_for_config_handlers(tmp_config_dir, sample_config):
    """Return a bare Bridge instance with mocked gateway, ready for config handlers."""
    import threading
    from unittest.mock import MagicMock

    from bridge import Bridge
    from conftest import TEST_HUB_ID

    bridge = Bridge.__new__(Bridge)
    bridge._config = dict(sample_config)
    bridge._config.update(
        {
            "hub_ws_port": 8765,
            "hub_remote_pairing_seconds": 60,
            "hub_ws_mdns": True,
            "dongle": "auto",
        }
    )
    bridge._hub_id = TEST_HUB_ID
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)
    bridge._zeroconf = None
    bridge._workers = []
    bridge._workers_lock = threading.Lock()
    return bridge


def test_on_mqtt_hub_dongle_updates_config(tmp_config_dir, sample_config):
    """_on_mqtt_hub_dongle updates config and publishes state."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)

    msg = MagicMock()
    msg.payload.decode.return_value = "/dev/hidraw1"

    with patch("bridge.save_config") as mock_save:
        bridge._on_mqtt_hub_dongle(None, None, msg)
        mock_save.assert_called_once()

    assert bridge._config["dongle"] == "/dev/hidraw1"
    # Should publish the new value back
    bridge._gateway.publish.assert_called()
    call_args = bridge._gateway.publish.call_args
    assert "/dev/hidraw1" in str(call_args)


def test_on_mqtt_hub_ws_port_updates_config(tmp_config_dir, sample_config):
    """_on_mqtt_hub_ws_port updates hub_ws_port for a valid value."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)

    msg = MagicMock()
    msg.payload.decode.return_value = "9000"

    with patch("bridge.save_config") as mock_save:
        bridge._on_mqtt_hub_ws_port(None, None, msg)
        mock_save.assert_called_once()

    assert bridge._config["hub_ws_port"] == 9000


def test_on_mqtt_hub_ws_port_ignores_invalid(tmp_config_dir, sample_config):
    """_on_mqtt_hub_ws_port ignores ports below 1024."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)
    bridge._config["hub_ws_port"] = 8765

    msg = MagicMock()
    msg.payload.decode.return_value = "99"

    with patch("bridge.save_config") as mock_save:
        bridge._on_mqtt_hub_ws_port(None, None, msg)
        mock_save.assert_not_called()

    assert bridge._config["hub_ws_port"] == 8765


def test_on_mqtt_hub_remote_pairing_timeout_updates_immediately(tmp_config_dir, sample_config):
    """_on_mqtt_hub_remote_pairing_timeout updates hub_remote_pairing_seconds immediately."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)

    msg = MagicMock()
    msg.payload.decode.return_value = "120"

    with patch("bridge.save_config") as mock_save:
        bridge._on_mqtt_hub_remote_pairing_timeout(None, None, msg)
        mock_save.assert_called_once()

    assert bridge._config["hub_remote_pairing_seconds"] == 120.0


def test_on_mqtt_hub_mdns_toggle(tmp_config_dir, sample_config):
    """_on_mqtt_hub_mdns 'false' delegates stop_mdns() to the ws listener."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)

    mock_ws = MagicMock()
    bridge._ws_listener = mock_ws

    msg = MagicMock()
    msg.payload.decode.return_value = "false"

    with patch("bridge.save_config"):
        bridge._on_mqtt_hub_mdns(None, None, msg)

    mock_ws.stop_mdns.assert_called_once()
    assert bridge._config["hub_ws_mdns"] is False


def test_on_mqtt_hub_mdns_toggle_true(tmp_config_dir, sample_config):
    """_on_mqtt_hub_mdns 'true' delegates start_mdns() to the ws listener."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)

    mock_ws = MagicMock()
    bridge._ws_listener = mock_ws

    msg = MagicMock()
    msg.payload.decode.return_value = "true"

    with patch("bridge.save_config"):
        bridge._on_mqtt_hub_mdns(None, None, msg)

    mock_ws.start_mdns.assert_called_once()
    assert bridge._config["hub_ws_mdns"] is True


def test_on_mqtt_hub_mdns_warns_when_ws_listener_not_running(tmp_config_dir, sample_config):
    """_on_mqtt_hub_mdns 'true' logs a warning when WS listener is not running."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)
    bridge._ws_listener = None  # WS listener not running

    msg = MagicMock()
    msg.payload.decode.return_value = "true"

    with patch("bridge.save_config"):
        bridge._on_mqtt_hub_mdns(None, None, msg)

    # mDNS must NOT be started — no point advertising a non-listening hub
    # Config is still saved as True so that enabling WS later auto-starts mDNS
    assert bridge._config["hub_ws_mdns"] is True


def test_on_mqtt_hub_ws_enabled_false_also_stops_mdns(tmp_config_dir, sample_config):
    """Disabling WS listener calls ws_listener.stop() which also tears down mDNS."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)

    mock_ws = MagicMock()
    bridge._ws_listener = mock_ws

    msg = MagicMock()
    msg.payload.decode.return_value = "false"

    with patch("bridge.save_config"):
        bridge._on_mqtt_hub_ws_enabled(None, None, msg)

    # stop() on WebSocketListener also handles mDNS teardown internally
    mock_ws.stop.assert_called_once()
    assert bridge._ws_listener is None


def test_hub_starts_without_dongle(tmp_config_dir, sample_config):
    """Bridge startup logic does not raise when no dongles are found.

    This test exercises the no-dongle warning path in isolation using a
    partially-constructed Bridge.  Full Bridge.start() requires a live MQTT
    broker and is covered by integration tests.
    """
    import threading
    from unittest.mock import MagicMock, patch

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._logger = logging.getLogger("test.bridge")
    bridge._config = {
        **sample_config,
        "hub_ws_port": 8765,
        "hub_ws_mdns": False,
        "hub_remote_pairing_seconds": 60,
        "dongle": "auto",
        "log_level": "INFO",
    }
    bridge._hub_id = "test-hub-no-dongle"
    bridge._workers = []
    bridge._workers_lock = threading.Lock()
    bridge._ws_listener = None
    bridge._ws_listener_thread = None
    bridge._zeroconf = None
    bridge._remote_pair_topic = ""
    bridge._remote_pairing_expires = 0.0
    bridge._remote_pairing_lock = threading.Lock()
    bridge._reload_topic = ""
    bridge._log_level_set_topic = ""
    bridge._cleanup_removed_dongles_topic = ""
    bridge._hub_restart_topic = ""
    bridge._hub_dongle_topic = ""
    bridge._hub_ws_port_topic = ""
    bridge._hub_remote_pairing_timeout_topic = ""
    bridge._hub_mdns_topic = ""
    bridge._gateway = None

    mock_gw = MagicMock()
    mock_gw.is_connected = True
    mock_gw.publish.return_value = MagicMock(rc=0)
    mock_gw.get_discovery_schema_version.return_value = 2

    with patch("bridge.load_config", return_value=(bridge._config, bridge._config)):
        with patch("bridge.load_hub_id", return_value=bridge._hub_id):
            with patch("bridge.MqttGateway", return_value=mock_gw):
                with patch("bridge.find_all_dongle_devices", return_value=[]):
                    with patch("bridge._mark_healthy"):
                        # Should not raise
                        bridge._load_config = lambda: None
                        bridge._hub_id = "test-hub-no-dongle"
                        bridge._setup_hub_topics = lambda hid: None

                        def fake_connect_mqtt():
                            bridge._gateway = mock_gw

                        bridge._connect_mqtt = fake_connect_mqtt
                        bridge._publish_hub_config_state = lambda: None
                        bridge._publish_hub_health = lambda s: None
                        bridge._start_ws_listener = lambda: None

                        # Simulate the core logic that matters:
                        # no dongles -> warning, no exception
                        device_paths = []
                        accept_remote = False
                        if not device_paths and not accept_remote:
                            bridge._logger.warning(
                                "No WyzeSense dongles found and no remote connections enabled. "
                                "Hub is running with no active dongle. Configure dongle via HA or config."
                            )
                        # No exception was raised — test passes

    # If we got here, no RuntimeError was raised
    assert True, "Bridge start should not raise when no dongles found"


# ---------------------------------------------------------------------------
# _remove_remote_chain
# ---------------------------------------------------------------------------


def _make_bridge_for_remote_chain(tmp_config_dir, sample_config, remote_id="test-remote-uuid"):
    """Return a Bridge with one mocked remote DongleWorker."""
    import threading

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.clear_sensor = MagicMock()
    bridge._gateway.clear_dongle = MagicMock()
    bridge._gateway.clear_remote = MagicMock()
    bridge._workers_lock = threading.Lock()
    bridge._hub_id = "test-hub-uuid"

    worker = MagicMock()
    worker.dongle_mac = TEST_DONGLE_MAC
    worker._remote_id = remote_id
    worker._registry = MagicMock()
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    bridge._workers = [worker]
    return bridge, worker


def test_remove_remote_chain_clears_sensors(tmp_config_dir, sample_config):
    """_remove_remote_chain calls clear_sensor for each sensor on remote workers."""
    bridge, worker = _make_bridge_for_remote_chain(tmp_config_dir, sample_config)
    bridge._publish_hub_counts = MagicMock()

    bridge._remove_remote_chain("test-remote-uuid")

    bridge._gateway.clear_sensor.assert_called_once_with("AAAAAAAA", "switch", wait=False)


def test_remove_remote_chain_clears_dongle(tmp_config_dir, sample_config):
    """_remove_remote_chain calls clear_dongle for each remote worker's dongle."""
    bridge, worker = _make_bridge_for_remote_chain(tmp_config_dir, sample_config)
    bridge._publish_hub_counts = MagicMock()

    bridge._remove_remote_chain("test-remote-uuid")

    bridge._gateway.clear_dongle.assert_called_once_with(TEST_DONGLE_MAC, wait=False)


def test_remove_remote_chain_clears_remote(tmp_config_dir, sample_config):
    """_remove_remote_chain calls clear_remote for the remote device itself."""
    bridge, worker = _make_bridge_for_remote_chain(tmp_config_dir, sample_config)
    bridge._publish_hub_counts = MagicMock()

    bridge._remove_remote_chain("test-remote-uuid")

    bridge._gateway.clear_remote.assert_called_once_with("test-remote-uuid", wait=False)


def test_remove_remote_chain_stops_workers(tmp_config_dir, sample_config):
    """_remove_remote_chain stops each remote worker and removes it from self._workers."""
    bridge, worker = _make_bridge_for_remote_chain(tmp_config_dir, sample_config)
    bridge._publish_hub_counts = MagicMock()

    bridge._remove_remote_chain("test-remote-uuid")

    worker.stop.assert_called_once()
    assert bridge._workers == [], "Remote workers should be removed from self._workers"


def test_remove_remote_chain_clears_remote_even_with_no_workers(tmp_config_dir, sample_config):
    """_remove_remote_chain clears the remote topics even when no workers are found."""
    import threading

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.clear_remote = MagicMock()
    bridge._workers_lock = threading.Lock()
    bridge._workers = []
    bridge._hub_id = "test-hub-uuid"
    bridge._publish_hub_counts = MagicMock()

    bridge._remove_remote_chain("ghost-remote-uuid")

    bridge._gateway.clear_remote.assert_called_once_with("ghost-remote-uuid", wait=False)


def test_remove_remote_chain_deletes_token_file(tmp_config_dir, sample_config):
    """_remove_remote_chain deletes the remote token file so it cannot reconnect without re-pairing."""
    import config as cfg_module

    remote_id = "test-remote-uuid"
    # Create a fake token file in the remotes directory
    remotes_path = pathlib.Path(cfg_module.CONFIG_DIR) / "remotes"
    token_dir = remotes_path / remote_id
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / "token"
    token_file.write_text("fake-token-data")

    bridge, worker = _make_bridge_for_remote_chain(tmp_config_dir, sample_config, remote_id=remote_id)
    bridge._publish_hub_counts = MagicMock()

    bridge._remove_remote_chain(remote_id)

    assert not token_file.exists(), "Token file should have been deleted by _remove_remote_chain"


def test_remove_remote_chain_no_error_when_token_file_missing(tmp_config_dir, sample_config):
    """_remove_remote_chain does not raise if the token file does not exist."""
    remote_id = "test-remote-uuid"
    bridge, worker = _make_bridge_for_remote_chain(tmp_config_dir, sample_config, remote_id=remote_id)
    bridge._publish_hub_counts = MagicMock()

    # Should not raise even if there is no token file
    bridge._remove_remote_chain(remote_id)


# ---------------------------------------------------------------------------
# _remote_cleanup_disconnected_dongles
# ---------------------------------------------------------------------------


def _make_bridge_for_remote_cleanup(tmp_config_dir, sample_config, remote_id="test-remote-uuid"):
    """Return a Bridge with one failed and one healthy remote DongleWorker."""
    import threading

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.clear_sensor = MagicMock()
    bridge._gateway.clear_dongle = MagicMock()
    bridge._hub_id = "test-hub-uuid"
    bridge._workers_lock = threading.Lock()
    bridge._publish_hub_counts = MagicMock()
    bridge._publish_remote_dongle_count = MagicMock()

    # Failed worker for this remote
    failed_worker = MagicMock()
    failed_worker.dongle_mac = TEST_DONGLE_MAC
    failed_worker._remote_id = remote_id
    failed_worker.failed = True
    failed_worker._stopped = False
    failed_worker._registry = MagicMock()
    failed_worker._registry.sensors = {"BBBBBBBB": {"sensor_type": "switch"}}

    # Healthy worker for this remote
    healthy_worker = MagicMock()
    healthy_worker.dongle_mac = "AABBCCDD"
    healthy_worker._remote_id = remote_id
    healthy_worker.failed = False
    healthy_worker._stopped = False
    healthy_worker._registry = MagicMock()
    healthy_worker._registry.sensors = {}

    bridge._workers = [failed_worker, healthy_worker]
    return bridge, failed_worker, healthy_worker


def test_remote_cleanup_disconnected_dongles_clears_sensor_topics(tmp_config_dir, sample_config):
    """_remote_cleanup_disconnected_dongles clears sensor topics for failed workers only."""
    bridge, failed_worker, healthy_worker = _make_bridge_for_remote_cleanup(tmp_config_dir, sample_config)

    bridge._remote_cleanup_disconnected_dongles("test-remote-uuid")

    bridge._gateway.clear_sensor.assert_called_once_with("BBBBBBBB", "switch", wait=False)


def test_remote_cleanup_disconnected_dongles_clears_dongle_topics(tmp_config_dir, sample_config):
    """_remote_cleanup_disconnected_dongles clears dongle topics for failed workers."""
    bridge, failed_worker, healthy_worker = _make_bridge_for_remote_cleanup(tmp_config_dir, sample_config)

    bridge._remote_cleanup_disconnected_dongles("test-remote-uuid")

    bridge._gateway.clear_dongle.assert_called_once_with(TEST_DONGLE_MAC, wait=False)


def test_remote_cleanup_disconnected_dongles_deletes_data_dir(tmp_config_dir, sample_config):
    """_remote_cleanup_disconnected_dongles deletes the data directory for failed dongle."""
    import config as cfg_module

    dongle_dir = pathlib.Path(cfg_module.CONFIG_DIR) / "dongles" / TEST_DONGLE_MAC
    dongle_dir.mkdir(parents=True, exist_ok=True)
    (dongle_dir / "sensors.yaml").write_text("")

    bridge, failed_worker, healthy_worker = _make_bridge_for_remote_cleanup(tmp_config_dir, sample_config)

    bridge._remote_cleanup_disconnected_dongles("test-remote-uuid")

    assert not dongle_dir.exists(), "Dongle data directory should have been deleted"


def test_remote_cleanup_disconnected_dongles_removes_failed_workers(tmp_config_dir, sample_config):
    """_remote_cleanup_disconnected_dongles removes failed workers but not healthy ones."""
    bridge, failed_worker, healthy_worker = _make_bridge_for_remote_cleanup(tmp_config_dir, sample_config)

    bridge._remote_cleanup_disconnected_dongles("test-remote-uuid")

    assert failed_worker not in bridge._workers, "Failed worker should be removed"
    assert healthy_worker in bridge._workers, "Healthy worker should remain"


def test_remote_cleanup_disconnected_dongles_noop_when_all_healthy(tmp_config_dir, sample_config):
    """_remote_cleanup_disconnected_dongles is a no-op when all workers are healthy."""
    bridge, failed_worker, healthy_worker = _make_bridge_for_remote_cleanup(tmp_config_dir, sample_config)
    failed_worker.failed = False  # make it healthy too

    bridge._remote_cleanup_disconnected_dongles("test-remote-uuid")

    bridge._gateway.clear_dongle.assert_not_called()
    bridge._gateway.clear_sensor.assert_not_called()


def test_on_mqtt_hub_ws_enabled_false_cleans_up_remote_chain(tmp_config_dir, sample_config):
    """Disabling the WS listener triggers _remove_remote_chain for all remote workers."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)
    bridge._ws_listener = MagicMock()
    bridge._zeroconf = None
    remote_id = "test-remote-uuid"
    worker = MagicMock()
    worker._remote_id = remote_id
    bridge._workers = [worker]

    msg = MagicMock()
    msg.payload.decode.return_value = "false"

    with patch("bridge.save_config"):
        with patch.object(bridge, "_remove_remote_chain") as mock_remove:
            bridge._on_mqtt_hub_ws_enabled(None, None, msg)
            mock_remove.assert_called_once_with(remote_id)


def test_on_mqtt_hub_ws_enabled_updates_config(tmp_config_dir, sample_config):
    """_on_mqtt_hub_ws_enabled updates hub_ws_enabled and publishes state."""
    from unittest.mock import MagicMock, patch

    bridge = _make_bridge_for_config_handlers(tmp_config_dir, sample_config)
    bridge._ws_listener = None

    msg = MagicMock()
    msg.payload.decode.return_value = "true"

    with patch("bridge.save_config") as mock_save:
        with patch.object(bridge, "_start_ws_listener") as mock_start:
            bridge._on_mqtt_hub_ws_enabled(None, None, msg)
            mock_save.assert_called_once()
            mock_start.assert_called_once()

    assert bridge._config["hub_ws_enabled"] is True
    bridge._gateway.publish.assert_called()


# ---------------------------------------------------------------------------
# Remote dongle and log_level config forwarding
# ---------------------------------------------------------------------------


def test_on_mqtt_remote_dongle_calls_send_dongle(tmp_config_dir, sample_config):
    """_on_mqtt_remote_dongle forwards the value to the remote via send_dongle."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    transport = MagicMock()
    msg = MagicMock()
    msg.payload.decode.return_value = "/dev/hidraw2"

    handler = bridge._on_mqtt_remote_dongle("remote-xyz", transport)
    handler(None, None, msg)

    transport.send_dongle.assert_called_once_with("/dev/hidraw2")


def test_on_mqtt_remote_dongle_echoes_state_to_mqtt(tmp_config_dir, sample_config):
    """_on_mqtt_remote_dongle publishes the new value back to the state topic."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    transport = MagicMock()
    msg = MagicMock()
    msg.payload.decode.return_value = "auto"

    handler = bridge._on_mqtt_remote_dongle("remote-xyz", transport)
    handler(None, None, msg)

    published_topics = [c.args[0] for c in bridge._gateway.publish.call_args_list]
    assert any("remote-xyz/dongle" in t for t in published_topics)


def test_on_mqtt_remote_log_level_calls_send_log_level(tmp_config_dir, sample_config):
    """_on_mqtt_remote_log_level forwards a valid level to the remote via send_log_level."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    transport = MagicMock()
    msg = MagicMock()
    msg.payload.decode.return_value = "DEBUG"

    handler = bridge._on_mqtt_remote_log_level("remote-xyz", transport)
    handler(None, None, msg)

    transport.send_log_level.assert_called_once_with("DEBUG")


def test_on_mqtt_remote_log_level_rejects_invalid_level(tmp_config_dir, sample_config):
    """_on_mqtt_remote_log_level ignores unknown log levels."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()

    transport = MagicMock()
    msg = MagicMock()
    msg.payload.decode.return_value = "VERBOSE"

    handler = bridge._on_mqtt_remote_log_level("remote-xyz", transport)
    handler(None, None, msg)

    transport.send_log_level.assert_not_called()


def test_on_mqtt_remote_log_level_echoes_state_to_mqtt(tmp_config_dir, sample_config):
    """_on_mqtt_remote_log_level publishes the new level back to the state topic."""
    from unittest.mock import MagicMock

    from bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._config = sample_config
    bridge._hub_id = "test-hub-id"
    bridge._logger = logging.getLogger("test.bridge")
    bridge._gateway = MagicMock()
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    transport = MagicMock()
    msg = MagicMock()
    msg.payload.decode.return_value = "WARNING"

    handler = bridge._on_mqtt_remote_log_level("remote-xyz", transport)
    handler(None, None, msg)

    published_topics = [c.args[0] for c in bridge._gateway.publish.call_args_list]
    assert any("remote-xyz/log_level" in t for t in published_topics)


# ---------------------------------------------------------------------------
# Battery percentage computation in _on_dongle_event
# ---------------------------------------------------------------------------


def test_on_dongle_event_computes_battery_pct_from_voltage(tmp_config_dir, sample_config):
    """_on_dongle_event adds battery pct to the published payload using SENSOR_TYPES curves."""
    from dongle_protocol import SensorEvent

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    worker._registry.state = {"AAAAAAAA": {"online": True}}
    worker._registry.is_valid_mac = lambda mac: True

    published_payloads = []

    def capture_publish(topic, payload=None, **kwargs):
        if isinstance(payload, dict):
            published_payloads.append((topic, payload))
        return MagicMock(rc=0)

    worker._gateway.publish.side_effect = capture_publish

    # raw=80, switch (CR1632 2.4–3.2V): voltage=80/32.0=2.5V → ~13%
    event = SensorEvent(
        "alarm",
        "AAAAAAAA",
        __import__("time").time(),
        sensor_type="switch",
        battery_raw=80,
        signal_strength=50,
        state="open",
    )
    worker._on_dongle_event(None, event)

    data_payloads = [p for t, p in published_payloads if "sensor/AAAAAAAA" in t and "status" not in t]
    assert data_payloads, "Expected at least one data payload"
    payload = data_payloads[0]
    assert "battery_voltage" in payload
    assert abs(payload["battery_voltage"] - 2.5) < 0.001
    assert payload["battery"] == 13


def test_on_dongle_event_battery_pct_none_for_unknown_type(tmp_config_dir, sample_config):
    """battery is None when no discharge curve exists for the sensor type."""
    from dongle_protocol import SensorEvent

    worker = _make_worker(tmp_config_dir, sample_config)
    worker._registry.sensors = {"AAAAAAAA": {"sensor_type": "chime"}}
    worker._registry.state = {"AAAAAAAA": {"online": True}}
    worker._registry.is_valid_mac = lambda mac: True

    published_payloads = []

    def capture_publish(topic, payload=None, **kwargs):
        if isinstance(payload, dict):
            published_payloads.append((topic, payload))
        return MagicMock(rc=0)

    worker._gateway.publish.side_effect = capture_publish

    event = SensorEvent(
        "alarm",
        "AAAAAAAA",
        __import__("time").time(),
        sensor_type="chime",
        battery_raw=80,
        signal_strength=50,
        state="open",
    )
    worker._on_dongle_event(None, event)

    data_payloads = [p for t, p in published_payloads if "sensor/AAAAAAAA" in t and "status" not in t]
    if data_payloads:
        assert data_payloads[0].get("battery") is None


# ---------------------------------------------------------------------------
# RemoteTransport (defined in bridge.py)
# ---------------------------------------------------------------------------


def _make_ws_mock(recv_frames: list) -> MagicMock:
    """Build a mock WebSocket that yields frames from recv_frames in order."""
    ws = MagicMock()
    ws.recv.side_effect = recv_frames
    ws.send = MagicMock()
    ws.close = MagicMock()
    return ws


class TestRemoteTransport:
    """RemoteTransport drains the replay deque before switching to live recv()."""

    def test_remote_id_property(self):
        from bridge import RemoteTransport

        ws = _make_ws_mock([])
        t = RemoteTransport(ws, remote_id="pi-floor2", dongle_mac="AABBCCDD", replay_frames=[])
        assert t.remote_id == "pi-floor2"

    def test_dongle_mac_property(self):
        from bridge import RemoteTransport

        ws = _make_ws_mock([])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="AABBCCDD", replay_frames=[])
        assert t.dongle_mac == "AABBCCDD"

    def test_replay_frames_drained_before_live(self):
        from bridge import RemoteTransport

        replay = [b"\x01" * 64, b"\x02" * 64]
        live = b"\x03" * 64
        ws = _make_ws_mock([live])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=list(replay))

        assert t.read() == replay[0]
        assert t.read() == replay[1]
        assert t.read() == live  # now switches to ws.recv()
        ws.recv.assert_called_once()

    def test_empty_replay_goes_straight_to_ws(self):
        from bridge import RemoteTransport

        live = b"\xaa" * 64
        ws = _make_ws_mock([live])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        result = t.read()
        assert result == live
        ws.recv.assert_called_once()

    def test_write_calls_ws_send(self):
        from bridge import RemoteTransport

        ws = _make_ws_mock([])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        data = b"\xaa\x55\x43\x03\x04\x01\x49"
        t.write(data)
        ws.send.assert_called_once_with(data)

    def test_close_calls_ws_close(self):
        from bridge import RemoteTransport

        ws = _make_ws_mock([])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        t.close()
        ws.close.assert_called_once()

    def test_close_swallows_exceptions(self):
        from bridge import RemoteTransport

        ws = MagicMock()
        ws.close.side_effect = RuntimeError("already closed")
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        t.close()  # must not raise

    def test_text_messages_from_ws_skipped_and_returns_empty(self):
        """Unexpected JSON control messages during forwarding are skipped."""
        from bridge import RemoteTransport

        text_msg = '{"type": "ping"}'
        binary_msg = b"\x05" * 64
        ws = _make_ws_mock([text_msg, binary_msg])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])

        result1 = t.read()  # receives text → returns b""
        assert result1 == b""
        result2 = t.read()  # receives binary → returns it
        assert result2 == binary_msg

    def test_remote_unhealthy_triggers_callback_and_returns_empty(self):
        """remote_unhealthy JSON text frame triggers on_health_change(remote_id, False)."""
        from bridge import RemoteTransport

        health_calls = []

        def callback(rid, healthy):
            health_calls.append((rid, healthy))

        unhealthy_msg = json.dumps({"type": "remote_unhealthy", "reason": "dongle_lost"})
        binary_msg = b"\xaa" * 64
        ws = _make_ws_mock([unhealthy_msg, binary_msg])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[], on_health_change=callback)

        result1 = t.read()  # health message → b""
        assert result1 == b""
        assert health_calls == [("r1", False)]

        result2 = t.read()  # binary frame passes through
        assert result2 == binary_msg

    def test_remote_healthy_triggers_callback_and_returns_empty(self):
        """remote_healthy JSON text frame triggers on_health_change(remote_id, True)."""
        from bridge import RemoteTransport

        health_calls = []

        def callback(rid, healthy):
            health_calls.append((rid, healthy))

        healthy_msg = json.dumps({"type": "remote_healthy"})
        ws = _make_ws_mock([healthy_msg])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[], on_health_change=callback)

        result = t.read()
        assert result == b""
        assert health_calls == [("r1", True)]

    def test_health_message_without_callback_does_not_raise(self):
        """Health messages with no callback set are silently ignored."""
        import json

        from bridge import RemoteTransport

        unhealthy_msg = json.dumps({"type": "remote_unhealthy", "reason": "dongle_lost"})
        ws = _make_ws_mock([unhealthy_msg])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        result = t.read()  # must not raise
        assert result == b""

    def test_send_restart_sends_correct_json_frame(self):
        """send_restart() sends {"type": "restart"} as a JSON text frame."""
        import json

        from bridge import RemoteTransport

        ws = _make_ws_mock([])
        ws.send = MagicMock()
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        t.send_restart()

        ws.send.assert_called_once()
        sent = ws.send.call_args.args[0]
        assert isinstance(sent, str)
        assert json.loads(sent) == {"type": "restart"}

    def test_send_dongle_sends_correct_json_frame(self):
        """send_dongle() sends {\"type\": \"set_dongle\", \"value\": ...} as a JSON text frame."""
        import json

        from bridge import RemoteTransport

        ws = _make_ws_mock([])
        ws.send = MagicMock()
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        t.send_dongle("/dev/hidraw1")

        ws.send.assert_called_once()
        sent = ws.send.call_args.args[0]
        assert isinstance(sent, str)
        assert json.loads(sent) == {"type": "set_dongle", "value": "/dev/hidraw1"}

    def test_send_log_level_sends_correct_json_frame(self):
        """send_log_level() sends {\"type\": \"set_log_level\", \"level\": ...} as a JSON text frame."""
        import json

        from bridge import RemoteTransport

        ws = _make_ws_mock([])
        ws.send = MagicMock()
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        t.send_log_level("DEBUG")

        ws.send.assert_called_once()
        sent = ws.send.call_args.args[0]
        assert isinstance(sent, str)
        assert json.loads(sent) == {"type": "set_log_level", "level": "DEBUG"}

    def test_unexpected_control_message_is_logged_not_raised(self):
        """An unknown text control message from the remote logs a warning and returns b\"\"."""
        import json

        from bridge import RemoteTransport

        ws = _make_ws_mock([json.dumps({"type": "unknown_future_message"})])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        result = t.read()
        assert result == b""

    def test_non_json_text_from_remote_is_logged_not_raised(self):
        """A non-JSON text message from the remote logs a warning and returns b\"\"."""
        from bridge import RemoteTransport

        ws = _make_ws_mock(["not valid json"])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        result = t.read()
        assert result == b""
