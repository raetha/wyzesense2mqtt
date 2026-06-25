"""
Tests for bridge.py — DongleWorker event handler logic and availability checking.

DongleWorker is tested with mocked MqttGateway, SensorRegistry, and Dongle
so no real hardware or broker is required.
"""

import logging
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
    worker._chime_subscribed = set()
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
