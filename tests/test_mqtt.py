"""
Tests for mqtt.py — discovery payload builders, MqttGateway publishing,
and discovery schema migration.

MqttGateway tests use unittest.mock to avoid needing a real broker.
"""

from unittest.mock import MagicMock, patch

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


def test_state_sensor_components_motion():
    from mqtt import _build_state_sensor_components

    components = _build_state_sensor_components("AAAAAAAA", {"sensor_type": "motion"}, "wyzesense2mqtt/AAAAAAAA")
    assert "state" in components
    state = components["state"]
    assert state["platform"] == "binary_sensor"
    assert state["device_class"] == "motion"
    assert state["payload_on"] == "active"
    assert state["payload_off"] == "inactive"


def test_state_sensor_components_contact():
    from mqtt import _build_state_sensor_components

    components = _build_state_sensor_components("AAAAAAAA", {"sensor_type": "switch"}, "wyzesense2mqtt/AAAAAAAA")
    state = components["state"]
    assert state["device_class"] == "opening"
    assert state["payload_on"] == "open"
    assert state["payload_off"] == "closed"


def test_state_sensor_components_contact_v2():
    from mqtt import _build_state_sensor_components

    components = _build_state_sensor_components("AAAAAAAA", {"sensor_type": "switchv2"}, "wyzesense2mqtt/AAAAAAAA")
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


def test_signal_strength_disabled_by_default():
    """signal_strength should be disabled by default (consistent with other HA integrations)."""
    from mqtt import _build_diagnostic_components

    components = _build_diagnostic_components()
    assert components["signal_strength"].get("enabled_by_default") is False
    # battery should remain enabled (users typically want low-battery warnings)
    assert "enabled_by_default" not in components["battery"]


def test_diagnostic_components_present():
    """_build_diagnostic_components returns fresh dicts with the expected keys."""
    from mqtt import _build_diagnostic_components

    components = _build_diagnostic_components()
    assert "signal_strength" in components
    assert "battery" in components
    assert components["signal_strength"]["device_class"] == "signal_strength"
    assert components["battery"]["device_class"] == "battery"
    assert components["signal_strength"]["entity_category"] == "diagnostic"


def test_diagnostic_components_returns_fresh_dicts():
    """_build_diagnostic_components must return a new dict on each call (no shared state)."""
    from mqtt import _build_diagnostic_components

    first = _build_diagnostic_components()
    second = _build_diagnostic_components()
    first["battery"]["unique_id"] = "wyzesense_AABBCCDD_battery"
    assert "unique_id" not in second["battery"]


def test_sensor_action_components_returns_fresh_dicts():
    """_build_sensor_action_components must return a new dict on each call (no shared state)."""
    from mqtt import _build_sensor_action_components

    first = _build_sensor_action_components("AABBCCDD", "ws2m/remove")
    second = _build_sensor_action_components("EEFFGGHH", "ws2m/remove")
    assert first["remove"]["payload_press"] == "AABBCCDD"
    assert second["remove"]["payload_press"] == "EEFFGGHH"
    first["remove"]["unique_id"] = "injected"
    assert "unique_id" not in second["remove"]


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
    calls = gw._client.publish.call_args_list
    topics = [c.args[0] for c in calls]
    assert any("homeassistant/device/wyzesense_AAAAAAAA/config" in t for t in topics)


def test_publish_sensor_discovery_payload_structure(tmp_config_dir):
    """Verify the discovery payload has the expected top-level keys."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion", "sw_version": "19"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=True)

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

    assert any("No discovery component builder" in r.message for r in caplog.records)
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

    for c in gw._client.publish.call_args_list:
        if "AAAAAAAA/status" in c.args[0]:
            assert c.kwargs["payload"] == "offline"
            break


# ---------------------------------------------------------------------------
# Sensor action components (remove button)
# ---------------------------------------------------------------------------


def test_sensor_discovery_includes_remove_button(tmp_config_dir):
    """Sensor discovery payload must include a remove button component."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/wyzesense_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            components = payload["components"]
            assert "remove" in components, "Expected 'remove' button component in sensor discovery"
            remove = components["remove"]
            assert remove["platform"] == "button"
            assert remove["command_topic"].endswith("/remove")
            assert remove["payload_press"] == "AAAAAAAA"
            assert remove["entity_category"] == "config"
            break
    else:
        pytest.fail("Discovery config publish call not found")


def test_sensor_discovery_button_has_no_value_template(tmp_config_dir):
    """Button components must not have value_template injected (no state topic)."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Front Door"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, "DONGLE01", sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/wyzesense_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            remove = payload["components"]["remove"]
            assert "value_template" not in remove, "Button component must not have value_template"
            assert "value_template" in payload["components"]["state"]
            assert "value_template" in payload["components"]["signal_strength"]
            break
    else:
        pytest.fail("Discovery config publish call not found")


# ---------------------------------------------------------------------------
# clear_sensor_topics
# ---------------------------------------------------------------------------


def test_clear_sensor_topics_publishes_none_payloads(tmp_config_dir):
    gw, cfg = _make_gateway()
    gw.clear_sensor_topics("AAAAAAAA", "switch")

    for c in gw._client.publish.call_args_list:
        payload = c.args[1] if len(c.args) > 1 else c.kwargs.get("payload")
        assert payload is None, f"Expected None payload for topic clear, got {payload!r}"


# ---------------------------------------------------------------------------
# Discovery schema migration — unified single key
# ---------------------------------------------------------------------------


def test_migrate_discovery_clears_v1_sensor_topics(tmp_config_dir):
    """v1→v2 migration must clear legacy per-entity sensor topics."""
    gw, cfg = _make_gateway()

    gw.migrate_discovery_topics("AAAAAAAA", "switch", "DONGLE01", from_version=1)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any("binary_sensor/wyzesense_AAAAAAAA" in t for t in topics)
    for c in gw._client.publish.call_args_list:
        assert c.kwargs["payload"] is None


def test_migrate_discovery_clears_v1_bridge_topic(tmp_config_dir):
    """v1→v2 migration must also clear the 3.x bridge discovery topic."""
    gw, cfg = _make_gateway()

    gw.migrate_discovery_topics("AAAAAAAA", "switch", "DONGLE01", from_version=1)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any("wyzesense_bridge_DONGLE01" in t for t in topics)


def test_migrate_discovery_noop_when_already_current(tmp_config_dir):
    from mqtt import DISCOVERY_SCHEMA_VERSION

    gw, cfg = _make_gateway()
    gw.migrate_discovery_topics("AAAAAAAA", "switch", "DONGLE01", from_version=DISCOVERY_SCHEMA_VERSION)

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
# Bridge discovery — device-based format
# ---------------------------------------------------------------------------


def test_publish_bridge_discovery(tmp_config_dir):
    """Bridge discovery uses the device-based format with a single retained topic."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_bridge_discovery("DONGLE01", "v1.2.3")

    assert gw._client.publish.called
    calls = gw._client.publish.call_args_list
    assert len(calls) == 1
    topic = calls[0].args[0]
    assert topic == "homeassistant/device/ws2m_bridge_DONGLE01/config"

    payload = json.loads(calls[0].kwargs["payload"])
    assert payload["device"]["name"] == "WyzeSense2MQTT Bridge DONGLE01"
    assert payload["device"]["hw_version"] == "v1.2.3"
    assert payload["origin"]["name"] == "WyzeSense2MQTT"

    components = payload["components"]
    assert "connection_state" in components
    assert "scan" in components
    assert "reload" in components

    conn = components["connection_state"]
    assert conn["platform"] == "binary_sensor"
    assert conn["device_class"] == "connectivity"
    assert conn["entity_category"] == "diagnostic"
    assert conn["has_entity_name"] is True
    assert "state_topic" in conn

    scan = components["scan"]
    assert scan["platform"] == "button"
    assert scan["entity_category"] == "config"
    assert scan["command_topic"].endswith("/scan")
    assert scan["payload_press"] == "scan"
    assert scan["has_entity_name"] is True

    reload_ = components["reload"]
    assert reload_["platform"] == "button"
    assert reload_["entity_category"] == "config"
    assert reload_["command_topic"].endswith("/reload")
    assert reload_["payload_press"] == "reload"
    assert reload_["has_entity_name"] is True


# ---------------------------------------------------------------------------
# clear_sensor_discovery_topics — module-level helper (used by maintenance CLI)
# ---------------------------------------------------------------------------


def test_clear_sensor_discovery_topics_v1_leak(tmp_config_dir):
    """Module-level helper clears all v1 per-entity topics for a leak sensor."""
    from mqtt import clear_sensor_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    config = {"hass_topic_root": "homeassistant", "mqtt_qos": 0, "mqtt_retain": True}

    clear_sensor_discovery_topics(client, config, None, "DDDDDDDD", "leak")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("probe_state" in t for t in topics)
    assert any("temperature" in t for t in topics)
    assert any("humidity" in t for t in topics)
    # v2 device topic also cleared
    assert any("device/wyzesense_DDDDDDDD/config" in t for t in topics)


def test_clear_sensor_discovery_topics_v1_climate(tmp_config_dir):
    """Climate sensor clear should include temperature and humidity, no binary state."""
    from mqtt import clear_sensor_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    config = {"hass_topic_root": "homeassistant", "mqtt_qos": 0, "mqtt_retain": True}

    clear_sensor_discovery_topics(client, config, None, "CCCCCCCC", "climate")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("temperature" in t for t in topics)
    assert any("humidity" in t for t in topics)
    assert not any("/state/" in t for t in topics)


def test_clear_sensor_discovery_topics_v1_binary(tmp_config_dir):
    """Binary sensor clear must include the state entity."""
    from mqtt import clear_sensor_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    config = {"hass_topic_root": "homeassistant", "mqtt_qos": 0, "mqtt_retain": True}

    clear_sensor_discovery_topics(client, config, None, "AAAAAAAA", "switch")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("/state/" in t for t in topics)
    assert not any("temperature" in t for t in topics)


# ---------------------------------------------------------------------------
# _publish — failed rc warning path
# ---------------------------------------------------------------------------


def test_publish_logs_warning_on_failed_rc():
    """_publish should log a warning when client.publish returns a non-success rc."""
    import logging
    import paho.mqtt.client as paho_mqtt
    from mqtt import _publish

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

    gw = MqttGateway({})
    gw._client = MagicMock()
    gw._client.connected_flag = True
    assert gw.is_connected is True


def test_is_connected_false_when_flag_unset():
    from mqtt import MqttGateway

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

    gw = MqttGateway({})
    mock_client = MagicMock()
    gw._client = mock_client
    assert gw.client is mock_client
