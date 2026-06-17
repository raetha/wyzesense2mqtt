"""
Tests for bridge.py — event handler logic and availability checking.

The Bridge is tested with mocked MqttGateway, SensorRegistry, and Dongle
so no real hardware or broker is required.
"""

import logging
import time
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge(tmp_config_dir, sample_config):
    """Return a Bridge whose subsystems are all mocked."""
    import config as cfg_module
    from bridge import Bridge

    bridge = Bridge(logging.getLogger("test"))
    bridge._config = sample_config
    bridge._initialized = True

    bridge._registry = MagicMock()
    bridge._registry.sensors = {}
    bridge._registry.state = {}
    bridge._registry.is_valid_mac = MagicMock(side_effect=lambda mac: len(mac) == 8 and mac != "00000000")

    bridge._gateway = MagicMock()
    bridge._gateway.is_connected = True
    bridge._gateway.publish.return_value = MagicMock(rc=0)

    bridge._dongle = MagicMock()
    bridge._dongle.mac = "DONGLE01"

    return bridge


def _make_event(mac="AAAAAAAA", event_type="alarm", sensor_type="switch", battery=80, signal_strength=-50, state="open", timestamp=None):
    """Build a synthetic SensorEvent-like object."""
    ev = MagicMock()
    ev.mac = mac
    ev.event = event_type
    ev.sensor_type = sensor_type
    ev.battery = battery
    ev.signal_strength = signal_strength
    ev.state = state
    ev.timestamp = timestamp or time.time()
    # Make vars(ev) work by using a real object instead
    from dongle_protocol import SensorEvent
    real_ev = SensorEvent(event_type, mac, ev.timestamp,
                          sensor_type=sensor_type, battery=battery,
                          signal_strength=signal_strength, state=state)
    return real_ev


# ---------------------------------------------------------------------------
# _on_dongle_event — basic flow
# ---------------------------------------------------------------------------


def test_on_dongle_event_ignores_string_messages(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._on_dongle_event(bridge._dongle, "some diagnostic string")
    bridge._gateway.publish.assert_not_called()


def test_on_dongle_event_ignores_when_not_initialized(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._initialized = False
    event = _make_event()
    bridge._on_dongle_event(bridge._dongle, event)
    bridge._gateway.publish.assert_not_called()


def test_on_dongle_event_rejects_invalid_mac(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.is_valid_mac = MagicMock(return_value=False)

    event = _make_event(mac="00000000")
    bridge._on_dongle_event(bridge._dongle, event)

    # Should attempt to delete the bad MAC from the dongle
    bridge._dongle.delete.assert_called_once_with("00000000")


def test_on_dongle_event_auto_adds_new_sensor(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": True}}

    event = _make_event(mac="AAAAAAAA", sensor_type="switch")
    bridge._on_dongle_event(bridge._dongle, event)

    bridge._registry.add_sensor.assert_called_once_with("AAAAAAAA", "switch")


def test_on_dongle_event_publishes_sensor_data(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch", "name": "Front Door"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": True}}
    bridge._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA")
    bridge._on_dongle_event(bridge._dongle, event)

    # Should publish to the sensor's data topic
    publish_topics = [c.args[0] for c in bridge._gateway.publish.call_args_list]
    assert any("wyzesense2mqtt/AAAAAAAA" == t for t in publish_topics)


def test_on_dongle_event_publishes_online_status(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": True}}
    bridge._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA")
    bridge._on_dongle_event(bridge._dongle, event)

    publish_calls = {c.args[0]: c.args[1] for c in bridge._gateway.publish.call_args_list}
    assert "wyzesense2mqtt/AAAAAAAA/status" in publish_calls
    assert publish_calls["wyzesense2mqtt/AAAAAAAA/status"] == "online"


def test_on_dongle_event_marks_offline_sensor_back_online(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10, "online": False}}
    bridge._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA")
    bridge._on_dongle_event(bridge._dongle, event)

    assert bridge._registry.state["AAAAAAAA"]["online"] is True


def test_on_dongle_event_updates_last_seen(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    old_ts = time.time() - 300
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "motion"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": old_ts, "online": True}}
    bridge._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA", event_type="alarm")
    bridge._on_dongle_event(bridge._dongle, event)

    assert bridge._registry.state["AAAAAAAA"]["last_seen"] > old_ts


def test_on_dongle_event_ignores_non_data_events(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time(), "online": True}}
    bridge._registry.update_sensor_type = MagicMock(return_value=False)

    event = _make_event(mac="AAAAAAAA", event_type="unknown:BB")
    bridge._on_dongle_event(bridge._dongle, event)

    # Status published but not data payload
    publish_topics = [c.args[0] for c in bridge._gateway.publish.call_args_list]
    assert "wyzesense2mqtt/AAAAAAAA/status" in publish_topics
    assert "wyzesense2mqtt/AAAAAAAA" not in publish_topics


def test_on_dongle_event_triggers_discovery_on_type_change(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time(), "online": True}}
    # update_sensor_type returns True → type changed
    bridge._registry.update_sensor_type = MagicMock(return_value=True)

    event = _make_event(mac="AAAAAAAA", sensor_type="switchv2")
    bridge._on_dongle_event(bridge._dongle, event)

    bridge._gateway.publish_sensor_discovery.assert_called_once()


def test_on_dongle_event_payload_includes_version_fields(tmp_config_dir, sample_config):
    import json

    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch", "name": "Front Door"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time(), "online": True}}
    bridge._registry.update_sensor_type = MagicMock(return_value=False)

    published_payloads = {}

    def _capture_publish(topic, payload, **kwargs):
        published_payloads[topic] = payload
        return MagicMock(rc=0)

    bridge._gateway.publish = _capture_publish

    event = _make_event(mac="AAAAAAAA")
    bridge._on_dongle_event(bridge._dongle, event)

    assert "wyzesense2mqtt/AAAAAAAA" in published_payloads
    data = published_payloads["wyzesense2mqtt/AAAAAAAA"]
    assert "ws2m_version" in data
    assert "ws2m_discovery_schema" in data


# ---------------------------------------------------------------------------
# _check_sensor_availability
# ---------------------------------------------------------------------------


def test_check_availability_marks_timed_out_sensor_offline(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    # Sensor last seen 10 hours ago; default timeout for motion is 8 h
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "motion"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 10 * 3600, "online": True}}
    bridge._registry.timeout_for = MagicMock(return_value=8 * 3600)

    bridge._check_sensor_availability()

    assert bridge._registry.state["AAAAAAAA"]["online"] is False
    publish_calls = {c.args[0]: c.args[1] for c in bridge._gateway.publish.call_args_list}
    assert publish_calls.get("wyzesense2mqtt/AAAAAAAA/status") == "offline"


def test_check_availability_does_not_mark_fresh_sensor_offline(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "motion"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 60, "online": True}}
    bridge._registry.timeout_for = MagicMock(return_value=8 * 3600)

    bridge._check_sensor_availability()

    assert bridge._registry.state["AAAAAAAA"]["online"] is True
    bridge._gateway.publish.assert_not_called()


def test_check_availability_skips_already_offline_sensors(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "motion"}}
    bridge._registry.state = {"AAAAAAAA": {"last_seen": time.time() - 99999, "online": False}}
    bridge._registry.timeout_for = MagicMock(return_value=8 * 3600)

    bridge._check_sensor_availability()

    # Already offline — should not publish again
    bridge._gateway.publish.assert_not_called()


# ---------------------------------------------------------------------------
# _remove_sensor
# ---------------------------------------------------------------------------


def test_remove_sensor_calls_dongle_delete(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}

    bridge._remove_sensor("AAAAAAAA")

    bridge._dongle.delete.assert_called_once_with("AAAAAAAA")


def test_remove_sensor_clears_mqtt_topics(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}

    bridge._remove_sensor("AAAAAAAA")

    bridge._gateway.clear_sensor_topics.assert_called_once()


def test_remove_sensor_updates_registry(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}

    bridge._remove_sensor("AAAAAAAA")

    bridge._registry.delete_sensor.assert_called_once_with("AAAAAAAA")


def test_remove_sensor_handles_dongle_timeout(tmp_config_dir, sample_config, caplog):
    import logging

    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    bridge._dongle.delete.side_effect = TimeoutError("dongle timed out")

    with caplog.at_level(logging.ERROR):
        bridge._remove_sensor("AAAAAAAA")  # should not raise

    assert any("timeout" in r.message.lower() for r in caplog.records)
    # Registry should still be cleaned up even if dongle failed
    bridge._registry.delete_sensor.assert_called_once_with("AAAAAAAA")


# ---------------------------------------------------------------------------
# MQTT command callbacks
# ---------------------------------------------------------------------------


def test_on_mqtt_scan_adds_sensor_on_success(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {}
    bridge._dongle.scan.return_value = ("AAAAAAAA", "switch", "19")

    msg = MagicMock()
    msg.payload.decode.return_value = ""
    bridge._on_mqtt_scan(None, None, msg)

    bridge._registry.add_sensor.assert_called_once_with("AAAAAAAA", "switch", "19")


def test_on_mqtt_scan_no_sensor_found(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._dongle.scan.return_value = None

    msg = MagicMock()
    msg.payload.decode.return_value = ""
    bridge._on_mqtt_scan(None, None, msg)

    bridge._registry.add_sensor.assert_not_called()


def test_on_mqtt_scan_skips_already_known_sensor(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}
    bridge._dongle.scan.return_value = ("AAAAAAAA", "switch", "19")

    msg = MagicMock()
    msg.payload.decode.return_value = ""
    bridge._on_mqtt_scan(None, None, msg)

    bridge._registry.add_sensor.assert_not_called()


def test_on_mqtt_remove(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.sensors = {"AAAAAAAA": {"sensor_type": "switch"}}

    msg = MagicMock()
    msg.payload.decode.return_value = "AAAAAAAA"
    bridge._on_mqtt_remove(None, None, msg)

    bridge._dongle.delete.assert_called_once_with("AAAAAAAA")
    bridge._registry.delete_sensor.assert_called_once_with("AAAAAAAA")


def test_on_mqtt_remove_invalid_mac(tmp_config_dir, sample_config):
    bridge = _make_bridge(tmp_config_dir, sample_config)
    bridge._registry.is_valid_mac = MagicMock(return_value=False)

    msg = MagicMock()
    msg.payload.decode.return_value = "00000000"
    bridge._on_mqtt_remove(None, None, msg)

    # delete is called to clean up the bad MAC from the dongle
    bridge._dongle.delete.assert_called_once()
    # but registry delete should NOT be called for an invalid mac
    bridge._registry.delete_sensor.assert_not_called()
