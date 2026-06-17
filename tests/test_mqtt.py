"""
Tests for mqtt.py — discovery payload builders, MqttGateway publishing,
and discovery schema migration.

MqttGateway tests use unittest.mock to avoid needing a real broker.
"""

from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway(config=None):
    """Return a MqttGateway with a mocked paho client."""
    from mqtt import MqttGateway

    cfg = config or {
        "mqtt_host": "testbroker.local",
        "mqtt_port": 1883,
        "mqtt_username": "user",
        "mqtt_password": "pass",
        "mqtt_client_id": "test",
        "mqtt_clean_session": False,
        "mqtt_keepalive": 60,
        "mqtt_qos": 0,
        "mqtt_retain": True,
        "self_topic_root": "wyzesense2mqtt",
        "hass_topic_root": "homeassistant",
        "hass_discovery": True,
        "publish_sensor_name": True,
        "usb_dongle": "auto",
    }
    gw = MqttGateway(cfg)
    gw._client = MagicMock()
    gw._client.connected_flag = True
    gw._client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    return gw, cfg


# ---------------------------------------------------------------------------
# Component builders — pure function shape tests
# ---------------------------------------------------------------------------


def test_binary_sensor_components_motion():
    from mqtt import _build_binary_sensor_components

    components = _build_binary_sensor_components("AAAAAAAA", {"sensor_type": "motion"}, "wyzesense2mqtt/AAAAAAAA")
    assert "state" in components
    state = components["state"]
    assert state["platform"] == "binary_sensor"
    assert state["device_class"] == "motion"
    assert state["payload_on"] == "active"
    assert state["payload_off"] == "inactive"


def test_binary_sensor_components_contact():
    from mqtt import _build_binary_sensor_components

    components = _build_binary_sensor_components("AAAAAAAA", {"sensor_type": "switch"}, "wyzesense2mqtt/AAAAAAAA")
    state = components["state"]
    assert state["device_class"] == "opening"
    assert state["payload_on"] == "open"
    assert state["payload_off"] == "closed"


def test_binary_sensor_components_contact_v2():
    from mqtt import _build_binary_sensor_components

    components = _build_binary_sensor_components("AAAAAAAA", {"sensor_type": "switchv2"}, "wyzesense2mqtt/AAAAAAAA")
    assert components["state"]["device_class"] == "opening"


def test_leak_sensor_components_structure():
    from mqtt import _build_leak_sensor_components

    components = _build_leak_sensor_components("DDDDDDDD", {"sensor_type": "leak"}, "wyzesense2mqtt/DDDDDDDD")
    assert "state" in components
    assert "probe_state" in components
    assert "temperature" in components
    assert "humidity" in components

    assert components["state"]["device_class"] == "moisture"
    assert components["probe_state"]["platform"] == "binary_sensor"
    assert components["temperature"]["unit_of_measurement"] == "°C"
    assert components["humidity"]["unit_of_measurement"] == "%"


def test_climate_sensor_components_structure():
    from mqtt import _build_climate_sensor_components

    components = _build_climate_sensor_components("CCCCCCCC", {"sensor_type": "climate"}, "wyzesense2mqtt/CCCCCCCC")
    assert "temperature" in components
    assert "humidity" in components
    assert components["temperature"]["unit_of_measurement"] == "°F"
    assert components["temperature"]["device_class"] == "temperature"
    assert components["humidity"]["device_class"] == "humidity"


def test_diagnostic_components_present():
    from mqtt import _DIAGNOSTIC_COMPONENTS

    assert "signal_strength" in _DIAGNOSTIC_COMPONENTS
    assert "battery" in _DIAGNOSTIC_COMPONENTS
    assert _DIAGNOSTIC_COMPONENTS["signal_strength"]["device_class"] == "signal_strength"
    assert _DIAGNOSTIC_COMPONENTS["battery"]["device_class"] == "battery"
    assert _DIAGNOSTIC_COMPONENTS["signal_strength"]["entity_category"] == "diagnostic"


def test_all_sensor_types_have_builders():
    """Every non-unknown sensor type must have a component builder."""
    from mqtt import _COMPONENT_BUILDERS
    from sensors import SENSOR_TYPES

    expected = set(SENSOR_TYPES.keys()) - {"unknown"}
    assert set(_COMPONENT_BUILDERS.keys()) == expected


# ---------------------------------------------------------------------------
# publish_sensor_discovery — payload shape
# ---------------------------------------------------------------------------


def test_publish_sensor_discovery_motion(tmp_config_dir):
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion", "sw_version": "19"}

    gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=True)

    assert gw._client.publish.called
    # Extract the discovery topic call
    calls = gw._client.publish.call_args_list
    topics = [c.args[0] for c in calls]
    assert any("homeassistant/device/wyzesense_AAAAAAAA/config" in t for t in topics)


def test_publish_sensor_discovery_payload_structure(tmp_config_dir):
    """Verify the discovery payload has the expected top-level keys."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion", "sw_version": "19"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=True)

    # Find the discovery config publish call
    for c in gw._client.publish.call_args_list:
        topic = c.args[0]
        if "device/wyzesense_AAAAAAAA/config" in topic:
            payload = json.loads(c.kwargs["payload"])
            assert "device" in payload
            assert "origin" in payload
            assert "components" in payload
            assert "availability" in payload
            assert "state_topic" in payload
            assert payload["origin"]["name"] == "WyzeSense2MQTT"
            assert "schema_version" in payload
            break
    else:
        pytest.fail("Discovery config publish call not found")


def test_publish_sensor_discovery_components_have_unique_ids(tmp_config_dir):
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/wyzesense_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            for key, component in payload["components"].items():
                assert "unique_id" in component, f"Component {key!r} missing unique_id"
                assert component["unique_id"] == f"wyzesense_AAAAAAAA_{key}"
            break


def test_publish_sensor_discovery_unknown_type_does_not_publish(tmp_config_dir, caplog):
    import logging

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "totally_unknown"}

    with caplog.at_level(logging.ERROR):
        gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=True)

    # Should log an error but not crash
    assert any("No discovery component builder" in r.message for r in caplog.records)
    # Should not have published a discovery config topic
    config_calls = [c for c in gw._client.publish.call_args_list if "device/wyzesense_AAAAAAAA/config" in c.args[0]]
    assert not config_calls


def test_publish_sensor_discovery_publishes_availability(tmp_config_dir):
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Front Door"}

    gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any("AAAAAAAA/status" in t for t in topics)


def test_publish_sensor_discovery_offline_sensor(tmp_config_dir):
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Back Door"}

    gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=False)

    # Find the status publish
    for c in gw._client.publish.call_args_list:
        if "AAAAAAAA/status" in c.args[0]:
            assert c.kwargs["payload"] == "offline"
            break


# ---------------------------------------------------------------------------
# clear_sensor_topics
# ---------------------------------------------------------------------------


def test_clear_sensor_topics_publishes_none_payloads(tmp_config_dir):
    gw, cfg = _make_gateway()
    gw.clear_sensor_topics("AAAAAAAA", "switch")

    # All published payloads should be None (clearing retained messages)
    for c in gw._client.publish.call_args_list:
        payload = c.args[1] if len(c.args) > 1 else c.kwargs.get("payload")
        assert payload is None, f"Expected None payload for topic clear, got {payload!r}"


# ---------------------------------------------------------------------------
# Discovery schema migration
# ---------------------------------------------------------------------------


def test_migrate_discovery_clears_v1_topics(tmp_config_dir):
    gw, cfg = _make_gateway()

    gw.migrate_discovery_topics("AAAAAAAA", "switch", from_version=1)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    # v1 per-entity topics should be cleared
    assert any("binary_sensor/wyzesense_AAAAAAAA" in t for t in topics)


def test_migrate_discovery_noop_when_already_current(tmp_config_dir):
    from mqtt import DISCOVERY_SCHEMA_VERSION

    gw, cfg = _make_gateway()
    # from_version == DISCOVERY_SCHEMA_VERSION means nothing to migrate
    gw.migrate_discovery_topics("AAAAAAAA", "switch", from_version=DISCOVERY_SCHEMA_VERSION)

    assert not gw._client.publish.called


def test_get_set_discovery_schema_version(tmp_config_dir):
    gw, _ = _make_gateway()

    gw.set_discovery_schema_version(2)
    assert gw.get_discovery_schema_version() == 2


def test_get_discovery_schema_version_defaults_to_1(tmp_config_dir):
    """Fresh installs with no migrations.yaml should default to 1."""
    gw, _ = _make_gateway()
    assert gw.get_discovery_schema_version() == 1


# ---------------------------------------------------------------------------
# Bridge discovery
# ---------------------------------------------------------------------------


def test_publish_bridge_discovery(tmp_config_dir):
    import json

    gw, cfg = _make_gateway()
    gw.publish_bridge_discovery("DONGLE01", "v1.2.3")

    assert gw._client.publish.called
    calls = gw._client.publish.call_args_list
    assert len(calls) == 1
    payload = json.loads(calls[0].kwargs["payload"])
    assert payload["device"]["name"] == "WyzeSense2MQTT Bridge DONGLE01"
    assert payload["device_class"] == "connectivity"
    assert payload["origin"]["name"] == "WyzeSense2MQTT"


# ---------------------------------------------------------------------------
# _clear_v1_discovery_topics — branch coverage for leak and climate types
# ---------------------------------------------------------------------------


def test_clear_v1_topics_for_leak_includes_probe_and_climate():
    """Leak sensor v1 clear should include probe_state, temperature, humidity."""
    from unittest.mock import MagicMock
    from mqtt import _clear_v1_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    config = {
        "hass_topic_root": "homeassistant",
        "mqtt_qos": 0,
        "mqtt_retain": True,
    }

    _clear_v1_discovery_topics(client, config, None, "DDDDDDDD", "leak")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("probe_state" in t for t in topics)
    assert any("temperature" in t for t in topics)
    assert any("humidity" in t for t in topics)


def test_clear_v1_topics_for_climate_includes_temperature_humidity():
    """Climate sensor (non-binary) v1 clear should include temperature and humidity."""
    from unittest.mock import MagicMock
    from mqtt import _clear_v1_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    config = {
        "hass_topic_root": "homeassistant",
        "mqtt_qos": 0,
        "mqtt_retain": True,
    }

    _clear_v1_discovery_topics(client, config, None, "CCCCCCCC", "climate")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("temperature" in t for t in topics)
    assert any("humidity" in t for t in topics)
    # Climate has no binary state entity
    assert not any("/state/" in t for t in topics)


def test_clear_v1_topics_for_binary_includes_state():
    """Binary sensor v1 clear should include the state entity."""
    from unittest.mock import MagicMock
    from mqtt import _clear_v1_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    config = {
        "hass_topic_root": "homeassistant",
        "mqtt_qos": 0,
        "mqtt_retain": True,
    }

    _clear_v1_discovery_topics(client, config, None, "AAAAAAAA", "switch")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("/state/" in t for t in topics)
    assert not any("temperature" in t for t in topics)


# ---------------------------------------------------------------------------
# _publish — failed rc warning path
# ---------------------------------------------------------------------------


def test_publish_logs_warning_on_failed_rc():
    """_publish should log a warning when client.publish returns a non-success rc."""
    import logging
    from unittest.mock import MagicMock, patch
    from mqtt import _publish
    import paho.mqtt.client as paho_mqtt

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=paho_mqtt.MQTT_ERR_NO_CONN)
    config = {"mqtt_qos": 0, "mqtt_retain": True}
    logger = logging.getLogger("test_publish")

    with patch.object(logger, "warning") as mock_warn:
        _publish(client, config, logger, "test/topic", {"key": "val"})
        assert mock_warn.called


# ---------------------------------------------------------------------------
# MqttGateway — client property raises when not connected
# ---------------------------------------------------------------------------


def test_gateway_client_property_raises_before_connect():
    from mqtt import MqttGateway

    gw = MqttGateway({"mqtt_host": "localhost"})
    with pytest.raises(RuntimeError, match="connect\\(\\)"):
        _ = gw.client


# ---------------------------------------------------------------------------
# MqttGateway.connect and disconnect — mocked paho client
# ---------------------------------------------------------------------------


def test_gateway_connect_sets_callbacks_and_connected_flag():
    """connect() should configure callbacks and wait for connected_flag."""
    import threading
    from unittest.mock import MagicMock, patch
    from mqtt import MqttGateway

    cfg = {
        "mqtt_host": "broker.local",
        "mqtt_port": 1883,
        "mqtt_username": "u",
        "mqtt_password": "p",
        "mqtt_client_id": "test",
        "mqtt_clean_session": False,
        "mqtt_keepalive": 60,
    }

    mock_client_instance = MagicMock()
    # Simulate connected_flag becoming True immediately after loop_start
    def _set_flag(*a, **kw):
        mock_client_instance.connected_flag = True
    mock_client_instance.loop_start.side_effect = _set_flag
    mock_client_instance.connected_flag = False

    on_connect = MagicMock()
    on_disconnect = MagicMock()
    on_message = MagicMock()

    with patch("mqtt.mqtt.Client", return_value=mock_client_instance):
        gw = MqttGateway(cfg)
        gw.connect(on_connect, on_disconnect, on_message)

    assert mock_client_instance.on_connect is on_connect
    assert mock_client_instance.on_disconnect is on_disconnect
    assert mock_client_instance.on_message is on_message
    mock_client_instance.connect_async.assert_called_once()
    mock_client_instance.loop_start.assert_called_once()


def test_gateway_disconnect_stops_loop_and_disconnects():
    from unittest.mock import MagicMock
    from mqtt import MqttGateway

    gw = MqttGateway({})
    gw._client = MagicMock()

    gw.disconnect()

    gw._client.loop_stop.assert_called_once()
    gw._client.disconnect.assert_called_once()


def test_gateway_disconnect_noop_when_not_connected():
    from mqtt import MqttGateway

    gw = MqttGateway({})
    gw._client = None
    gw.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# MqttGateway.is_connected
# ---------------------------------------------------------------------------


def test_is_connected_true_when_flag_set():
    from mqtt import MqttGateway
    from unittest.mock import MagicMock

    gw = MqttGateway({})
    gw._client = MagicMock()
    gw._client.connected_flag = True
    assert gw.is_connected is True


def test_is_connected_false_when_flag_unset():
    from mqtt import MqttGateway
    from unittest.mock import MagicMock

    gw = MqttGateway({})
    gw._client = MagicMock()
    gw._client.connected_flag = False
    assert gw.is_connected is False


def test_is_connected_false_when_no_client():
    from mqtt import MqttGateway

    gw = MqttGateway({})
    gw._client = None
    assert gw.is_connected is False


# ---------------------------------------------------------------------------
# MqttGateway.clear_all_discovery_topics — calls all cleaners
# ---------------------------------------------------------------------------


def test_clear_all_discovery_topics_calls_all_schema_cleaners():
    """clear_all_discovery_topics should invoke every registered cleaner."""
    from unittest.mock import MagicMock, patch
    from mqtt import MqttGateway, _DISCOVERY_CLEANERS

    gw, _ = _make_gateway()
    called_versions = []

    def _make_mock_cleaner(version):
        def _cleaner(client, config, logger, mac, sensor_type, wait=True):
            called_versions.append(version)
        return _cleaner

    mock_cleaners = {v: _make_mock_cleaner(v) for v in _DISCOVERY_CLEANERS}
    with patch("mqtt._DISCOVERY_CLEANERS", mock_cleaners):
        gw.clear_all_discovery_topics("AAAAAAAA", "switch")

    assert set(called_versions) == set(_DISCOVERY_CLEANERS.keys())


# ---------------------------------------------------------------------------
# Packet __str__ and SensorEvent __str__
# ---------------------------------------------------------------------------


def test_packet_str_normal():
    from dongle_protocol import Packet

    pkt = Packet.get_sensor_count()
    s = str(pkt)
    assert "Cmd=" in s


def test_packet_str_ack():
    from dongle_protocol import Packet

    pkt = Packet.async_ack(Packet.CMD_GET_DONGLE_VERSION)
    s = str(pkt)
    assert "ACK" in s


def test_sensor_event_str():
    from dongle_protocol import SensorEvent

    ev = SensorEvent("alarm", "AAAAAAAA", 1700000000.0, sensor_type="switch", state="open", battery=80)
    s = str(ev)
    assert "alarm" in s
    assert "AAAAAAAA" in s
    assert "open" in s


def test_gateway_client_property_returns_client_when_connected():
    from mqtt import MqttGateway
    from unittest.mock import MagicMock

    gw = MqttGateway({})
    mock_client = MagicMock()
    gw._client = mock_client
    assert gw.client is mock_client
