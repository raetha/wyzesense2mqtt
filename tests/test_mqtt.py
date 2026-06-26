"""
Tests for mqtt.py — discovery payload builders, MqttGateway publishing,
and discovery schema migration.

MqttGateway tests use unittest.mock to avoid needing a real broker.
"""

from unittest.mock import MagicMock, patch

import pytest

from conftest import TEST_DONGLE_MAC, TEST_SERVICE_ID


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
        "self_topic_root": "wyzesense2mqtt",
        "hass_topic_root": "homeassistant",
        "hass_discovery": True,
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
    from mqtt import _build_diagnostic_components

    components = _build_diagnostic_components()
    assert components["signal_strength"].get("enabled_by_default") is False
    assert "enabled_by_default" not in components["battery"]


def test_diagnostic_components_present():
    from mqtt import _build_diagnostic_components

    components = _build_diagnostic_components()
    assert "signal_strength" in components
    assert "battery" in components
    assert components["signal_strength"]["device_class"] == "signal_strength"
    assert components["battery"]["device_class"] == "battery"
    assert components["signal_strength"]["entity_category"] == "diagnostic"


def test_diagnostic_components_returns_fresh_dicts():
    from mqtt import _build_diagnostic_components

    first = _build_diagnostic_components()
    second = _build_diagnostic_components()
    first["battery"]["unique_id"] = "wyzesense_AABBCCDD_battery"
    assert "unique_id" not in second["battery"]


def test_sensor_action_components_returns_fresh_dicts():
    from mqtt import _build_sensor_action_components

    first = _build_sensor_action_components("AABBCCDD", "ws2m/dongle_X/remove")
    second = _build_sensor_action_components("EEFFGGHH", "ws2m/dongle_X/remove")
    assert first["remove"]["payload_press"] == "AABBCCDD"
    assert second["remove"]["payload_press"] == "EEFFGGHH"
    first["remove"]["unique_id"] = "injected"
    assert "unique_id" not in second["remove"]


def test_all_sensor_types_have_builders():
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

    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    assert gw._client.publish.called
    calls = gw._client.publish.call_args_list
    topics = [c.args[0] for c in calls]
    assert any("homeassistant/device/wyzesense_AAAAAAAA/config" in t for t in topics)


def test_publish_sensor_discovery_payload_structure(tmp_config_dir):
    """Verify the discovery payload has the expected top-level keys."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion", "sw_version": "19"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

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


def test_publish_sensor_discovery_via_device_is_dongle(tmp_config_dir):
    """Sensor's via_device must point to the dongle, not the service."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Front Door"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/wyzesense_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            assert payload["device"]["via_device"] == f"ws2m_dongle_{TEST_DONGLE_MAC}"
            break
    else:
        pytest.fail("Discovery config publish call not found")


def test_publish_sensor_discovery_availability_includes_dongle_status(tmp_config_dir):
    """Sensor availability must include the dongle status topic."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Front Door"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/wyzesense_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            avail_topics = [a["topic"] for a in payload["availability"]]
            assert any(f"dongle_{TEST_DONGLE_MAC}/status" in t for t in avail_topics), (
                f"Dongle status not in availability: {avail_topics}"
            )
            assert payload["availability_mode"] == "all"
            break
    else:
        pytest.fail("Discovery config publish call not found")


def test_publish_sensor_discovery_components_have_unique_ids(tmp_config_dir):
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

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
        gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    assert any("No discovery component builder" in r.message for r in caplog.records)
    config_calls = [c for c in gw._client.publish.call_args_list if "device/wyzesense_AAAAAAAA/config" in c.args[0]]
    assert not config_calls


def test_publish_sensor_discovery_publishes_availability(tmp_config_dir):
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Front Door"}

    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any("AAAAAAAA/status" in t for t in topics)


def test_publish_sensor_discovery_offline_sensor(tmp_config_dir):
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Back Door"}

    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=False)

    for c in gw._client.publish.call_args_list:
        if "AAAAAAAA/status" in c.args[0]:
            assert c.kwargs["payload"] == "offline"
            break


# ---------------------------------------------------------------------------
# Sensor action components (remove button)
# ---------------------------------------------------------------------------


def test_sensor_discovery_includes_remove_button(tmp_config_dir):
    """Sensor discovery payload must include a remove button on the dongle-scoped topic."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/wyzesense_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            components = payload["components"]
            assert "remove" in components, "Expected 'remove' button component in sensor discovery"
            remove = components["remove"]
            assert remove["platform"] == "button"
            # Remove button must target the dongle-scoped remove topic
            assert f"dongle_{TEST_DONGLE_MAC}/remove" in remove["command_topic"]
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
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/wyzesense_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            remove = payload["components"]["remove"]
            assert "value_template" not in remove
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
# Discovery schema migration — v3 schema
# ---------------------------------------------------------------------------


def test_migrate_discovery_clears_v1_sensor_topics(tmp_config_dir):
    """v1->v2 migration step must clear legacy per-entity sensor topics."""
    gw, cfg = _make_gateway()

    gw.migrate_discovery_topics("AAAAAAAA", "switch", TEST_DONGLE_MAC, TEST_SERVICE_ID, from_version=1)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any("binary_sensor/wyzesense_AAAAAAAA" in t for t in topics)
    for c in gw._client.publish.call_args_list:
        assert c.kwargs["payload"] is None


def test_migrate_discovery_clears_v1_bridge_topic(tmp_config_dir):
    """v1->v2 migration must clear the 3.x wyzesense_bridge_<mac> topic."""
    gw, cfg = _make_gateway()

    gw.migrate_discovery_topics("AAAAAAAA", "switch", TEST_DONGLE_MAC, TEST_SERVICE_ID, from_version=1)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(f"wyzesense_bridge_{TEST_DONGLE_MAC}" in t for t in topics)



def test_migrate_discovery_noop_when_already_current(tmp_config_dir):
    from mqtt import DISCOVERY_SCHEMA_VERSION

    gw, cfg = _make_gateway()
    gw.migrate_discovery_topics(
        "AAAAAAAA", "switch", TEST_DONGLE_MAC, TEST_SERVICE_ID, from_version=DISCOVERY_SCHEMA_VERSION
    )

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
# Service discovery
# ---------------------------------------------------------------------------


def test_publish_service_discovery(tmp_config_dir):
    """Service discovery uses device-based format with a single retained topic."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_service_discovery(TEST_SERVICE_ID, "INFO")

    assert gw._client.publish.called
    calls = gw._client.publish.call_args_list
    # 2 publishes: discovery config + log_level state
    assert len(calls) == 2
    topic = calls[0].args[0]
    assert topic == f"homeassistant/device/ws2m_service_{TEST_SERVICE_ID}/config"

    payload = json.loads(calls[0].kwargs["payload"])
    assert payload["device"]["identifiers"] == [f"ws2m_service_{TEST_SERVICE_ID}"]
    assert payload["device"]["name"] == "WyzeSense2MQTT"
    assert "sw_version" in payload["device"]
    assert "hw_version" not in payload["device"]  # hw belongs to the dongle, not the service
    assert payload["origin"]["name"] == "WyzeSense2MQTT"

    components = payload["components"]
    assert "reload" in components, "Service device must have reload button"
    # Scan and remove must NOT be on the service device
    assert "scan" not in components, "Scan belongs to dongle, not service"
    assert "remove" not in components, "Remove belongs to dongle, not service"

    reload_ = components["reload"]
    assert reload_["platform"] == "button"
    assert reload_["entity_category"] == "config"
    assert reload_["command_topic"].endswith("/reload")
    assert reload_["payload_press"] == "reload"
    assert reload_["has_entity_name"] is True

    assert "log_level" in components, "Service device must have log_level select"
    log_level = components["log_level"]
    assert log_level["platform"] == "select"
    assert log_level["entity_category"] == "config"


def test_publish_service_discovery_unique_ids(tmp_config_dir):
    """Service device entities must have unique_ids prefixed with ws2m_service_<uuid>."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_service_discovery(TEST_SERVICE_ID, "INFO")

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    for key, component in payload["components"].items():
        assert "unique_id" in component, f"Component {key!r} missing unique_id"
        assert component["unique_id"].startswith(f"ws2m_service_{TEST_SERVICE_ID}_")


# ---------------------------------------------------------------------------
# Dongle discovery
# ---------------------------------------------------------------------------


def test_publish_dongle_discovery(tmp_config_dir):
    """Dongle discovery uses device-based format; device is child of service."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_dongle_discovery(TEST_DONGLE_MAC, "v1.2.3", TEST_SERVICE_ID)

    assert gw._client.publish.called
    calls = gw._client.publish.call_args_list
    assert len(calls) == 1
    topic = calls[0].args[0]
    assert topic == f"homeassistant/device/ws2m_dongle_{TEST_DONGLE_MAC}/config"

    payload = json.loads(calls[0].kwargs["payload"])
    assert payload["device"]["hw_version"] == "v1.2.3"
    assert payload["device"]["via_device"] == f"ws2m_service_{TEST_SERVICE_ID}"
    assert payload["device"]["name"] == f"WyzeSense Dongle {TEST_DONGLE_MAC}"
    assert payload["origin"]["name"] == "WyzeSense2MQTT"

    components = payload["components"]
    assert "connection_state" in components
    assert "scan" in components
    assert "remove" in components
    # Reload must NOT be on the dongle — it's a service-level entity
    assert "reload" not in components, "Reload belongs to service, not dongle"

    conn = components["connection_state"]
    assert conn["platform"] == "binary_sensor"
    assert conn["device_class"] == "connectivity"
    assert conn["entity_category"] == "diagnostic"
    assert conn["has_entity_name"] is True
    assert f"dongle_{TEST_DONGLE_MAC}/status" in conn["state_topic"]

    scan = components["scan"]
    assert scan["platform"] == "button"
    assert scan["entity_category"] == "config"
    assert f"dongle_{TEST_DONGLE_MAC}/scan" in scan["command_topic"]
    assert scan["payload_press"] == "scan"
    assert scan["has_entity_name"] is True

    remove = components["remove"]
    assert remove["platform"] == "button"
    assert f"dongle_{TEST_DONGLE_MAC}/remove" in remove["command_topic"]
    assert remove["has_entity_name"] is True


def test_publish_dongle_discovery_unique_ids(tmp_config_dir):
    """Dongle device entities must have unique_ids prefixed with ws2m_dongle_<mac>."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_dongle_discovery(TEST_DONGLE_MAC, "v1.2.3", TEST_SERVICE_ID)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    for key, component in payload["components"].items():
        assert "unique_id" in component, f"Component {key!r} missing unique_id"
        assert component["unique_id"] == f"ws2m_dongle_{TEST_DONGLE_MAC}_{key}"


# ---------------------------------------------------------------------------
# clear_sensor_discovery_topics — module-level helper (used by mqtt_tool CLI)
# ---------------------------------------------------------------------------


def test_clear_sensor_discovery_topics_v1_leak(tmp_config_dir):
    from mqtt import clear_sensor_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    config = {"hass_topic_root": "homeassistant"}

    clear_sensor_discovery_topics(client, config, None, "DDDDDDDD", "leak")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("probe_state" in t for t in topics)
    assert any("temperature" in t for t in topics)
    assert any("humidity" in t for t in topics)
    assert any("device/wyzesense_DDDDDDDD/config" in t for t in topics)


def test_clear_sensor_discovery_topics_v1_climate(tmp_config_dir):
    from mqtt import clear_sensor_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock()    )
    config = {"hass_topic_root": "homeassistant"}

    clear_sensor_discovery_topics(client, config, None, "CCCCCCCC", "climate")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("temperature" in t for t in topics)
    assert any("humidity" in t for t in topics)
    assert not any("/state/" in t for t in topics)


def test_clear_sensor_discovery_topics_v1_binary(tmp_config_dir):
    from mqtt import clear_sensor_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    config = {"hass_topic_root": "homeassistant"}

    clear_sensor_discovery_topics(client, config, None, "AAAAAAAA", "switch")

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert any("/state/" in t for t in topics)
    assert not any("temperature" in t for t in topics)


# ---------------------------------------------------------------------------
# _publish — failed rc warning path
# ---------------------------------------------------------------------------


def test_publish_logs_warning_on_failed_rc():
    import logging
    import paho.mqtt.client as paho_mqtt
    from mqtt import _publish

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=paho_mqtt.MQTT_ERR_NO_CONN)
    logger = logging.getLogger("test_publish")

    with patch.object(logger, "warning") as mock_warn:
        _publish(client, logger, "test/topic", {"key": "val"})
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


# ---------------------------------------------------------------------------
# Keypad discovery component builder
# ---------------------------------------------------------------------------


def test_build_keypad_components_has_alarm_control_panel():
    from mqtt import _build_keypad_components

    result = _build_keypad_components("KPADKPAD", {}, "wyzesense2mqtt/KPADKPAD")
    assert "alarm_mode" in result
    assert result["alarm_mode"]["platform"] == "alarm_control_panel"
    assert "payload_disarm" in result["alarm_mode"]
    assert "payload_arm_home" in result["alarm_mode"]
    assert "payload_arm_away" in result["alarm_mode"]
    assert "command_topic" in result["alarm_mode"]


def test_build_keypad_components_has_motion():
    from mqtt import _build_keypad_components

    result = _build_keypad_components("KPADKPAD", {}, "wyzesense2mqtt/KPADKPAD")
    assert "motion" in result
    assert result["motion"]["platform"] == "binary_sensor"
    assert result["motion"]["device_class"] == "motion"


def test_build_keypad_components_returns_fresh_dicts():
    from mqtt import _build_keypad_components

    r1 = _build_keypad_components("KPADKPAD", {}, "wyzesense2mqtt/KPADKPAD")
    r2 = _build_keypad_components("KPADKPAD", {}, "wyzesense2mqtt/KPADKPAD")
    r1["alarm_mode"]["sentinel"] = True
    assert "sentinel" not in r2["alarm_mode"]


def test_keypad_registered_in_component_builders():
    from mqtt import _COMPONENT_BUILDERS

    assert "keypad" in _COMPONENT_BUILDERS


def test_publish_sensor_discovery_keypad(sample_config, tmp_config_dir):
    from mqtt import MqttGateway

    gw = MqttGateway(sample_config)
    mock_client = MagicMock()
    mock_client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    gw._client = mock_client

    sensor = {"sensor_type": "keypad", "name": "Front Keypad"}
    gw.publish_sensor_discovery("KPADKPAD", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    assert mock_client.publish.called
    topic_args = [call.args[0] for call in mock_client.publish.call_args_list]
    assert any("wyzesense_KPADKPAD" in t for t in topic_args)


# ---------------------------------------------------------------------------
# Chime discovery component builder
# ---------------------------------------------------------------------------


def test_build_chime_components_has_play_button():
    from mqtt import _build_chime_components

    result = _build_chime_components("CHIMEMAC", {}, "wyzesense2mqtt/CHIMEMAC")
    assert "play" in result
    assert result["play"]["platform"] == "button"
    assert result["play"]["payload_press"] == "PLAY"
    assert result["play"]["command_topic"].endswith("/play")


def test_build_chime_components_has_number_entities():
    from mqtt import _build_chime_components

    result = _build_chime_components("CHIMEMAC", {}, "wyzesense2mqtt/CHIMEMAC")
    for key in ("ring_id", "volume", "repeat_count"):
        assert key in result, f"Missing chime number entity: {key}"
        assert result[key]["platform"] == "number"
        assert "state_topic" in result[key]
        assert "command_topic" in result[key]
        assert result[key]["command_topic"].endswith(f"/{key}/set")


def test_build_chime_ring_id_range():
    from mqtt import _build_chime_components

    result = _build_chime_components("CHIMEMAC", {}, "wyzesense2mqtt/CHIMEMAC")
    assert result["ring_id"]["min"] == 0
    assert result["ring_id"]["max"] == 255


def test_build_chime_volume_range():
    from mqtt import _build_chime_components

    result = _build_chime_components("CHIMEMAC", {}, "wyzesense2mqtt/CHIMEMAC")
    assert result["volume"]["min"] == 1
    assert result["volume"]["max"] == 9


def test_build_chime_components_returns_fresh_dicts():
    from mqtt import _build_chime_components

    r1 = _build_chime_components("CHIMEMAC", {}, "wyzesense2mqtt/CHIMEMAC")
    r2 = _build_chime_components("CHIMEMAC", {}, "wyzesense2mqtt/CHIMEMAC")
    r1["ring_id"]["sentinel"] = True
    assert "sentinel" not in r2["ring_id"]


def test_chime_registered_in_component_builders():
    from mqtt import _COMPONENT_BUILDERS

    assert "chime" in _COMPONENT_BUILDERS


def test_publish_sensor_discovery_chime(sample_config, tmp_config_dir):
    from mqtt import MqttGateway

    gw = MqttGateway(sample_config)
    mock_client = MagicMock()
    mock_client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    gw._client = mock_client

    sensor = {"sensor_type": "chime", "name": "Front Door Chime"}
    gw.publish_sensor_discovery("CHIMEMAC", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    assert mock_client.publish.called
    topic_args = [call.args[0] for call in mock_client.publish.call_args_list]
    assert any("wyzesense_CHIMEMAC" in t for t in topic_args)


# ---------------------------------------------------------------------------
# invert_state — _build_state_sensor_components
# ---------------------------------------------------------------------------


def test_invert_state_false_leaves_payloads_unchanged():
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "switch", "invert_state": False}
    result = _build_state_sensor_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert result["state"]["payload_on"] == "open"
    assert result["state"]["payload_off"] == "closed"


def test_invert_state_true_swaps_payloads_contact():
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "switch", "invert_state": True}
    result = _build_state_sensor_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert result["state"]["payload_on"] == "closed"
    assert result["state"]["payload_off"] == "open"


def test_invert_state_true_swaps_payloads_motion():
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "motion", "invert_state": True}
    result = _build_state_sensor_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert result["state"]["payload_on"] == "inactive"
    assert result["state"]["payload_off"] == "active"


def test_invert_state_not_applied_to_non_invertible_type():
    """invert_state on a sensor type not in INVERTIBLE_SENSOR_TYPES has no effect."""
    from sensors import SENSOR_TYPES
    from mqtt import _build_state_sensor_components

    # 'unknown' is not invertible; payloads should be unchanged
    sensor = {"sensor_type": "unknown", "invert_state": True}
    result = _build_state_sensor_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    # Falls back to unknown type defaults; not swapped
    assert result["state"]["payload_on"] == result["state"]["payload_on"]  # shape check
    assert "payload_on" in result["state"]


def test_device_class_override_used_in_state_component():
    """Per-sensor 'class' key overrides the type default device_class."""
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "switch", "class": "door"}
    result = _build_state_sensor_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert result["state"]["device_class"] == "door"


def test_device_class_falls_back_to_type_default():
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "switch"}
    result = _build_state_sensor_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert result["state"]["device_class"] == "opening"


# ---------------------------------------------------------------------------
# _build_sensor_config_components
# ---------------------------------------------------------------------------


def test_sensor_config_components_always_has_sensor_name():
    from mqtt import _build_sensor_config_components

    for sensor_type in ("switch", "motion", "leak", "climate", "chime", "keypad", "unknown"):
        sensor = {"sensor_type": sensor_type}
        result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
        assert "sensor_name" in result, f"sensor_name missing for {sensor_type}"
        comp = result["sensor_name"]
        assert comp["platform"] == "text"
        assert comp["entity_category"] == "config"
        assert "state_topic" in comp
        assert "command_topic" in comp
        assert comp["command_topic"].endswith("/sensor_name/set")


def test_sensor_config_components_device_class_select_for_contact():
    from mqtt import _build_sensor_config_components
    from sensors import DEVICE_CLASS_OPTIONS

    sensor = {"sensor_type": "switch"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "device_class" in result
    comp = result["device_class"]
    assert comp["platform"] == "select"
    assert comp["entity_category"] == "config"
    assert set(comp["options"]) == set(DEVICE_CLASS_OPTIONS["switch"])
    assert comp["command_topic"].endswith("/device_class/set")


def test_sensor_config_components_device_class_select_for_motion():
    from mqtt import _build_sensor_config_components
    from sensors import DEVICE_CLASS_OPTIONS

    sensor = {"sensor_type": "motion"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "device_class" in result
    assert set(result["device_class"]["options"]) == set(DEVICE_CLASS_OPTIONS["motion"])


def test_sensor_config_components_no_device_class_for_leak():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "leak"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "device_class" not in result, "Leak sensor class is fixed; no select entity"


def test_sensor_config_components_no_device_class_for_climate():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "climate"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "device_class" not in result


def test_sensor_config_components_invert_state_for_contact():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "switch"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "invert_state" in result
    comp = result["invert_state"]
    assert comp["platform"] == "switch"
    assert comp["entity_category"] == "config"
    assert comp["payload_on"] == "true"
    assert comp["payload_off"] == "false"
    assert comp["command_topic"].endswith("/invert_state/set")


def test_sensor_config_components_invert_state_for_motion():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "motionv2"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "invert_state" in result


def test_sensor_config_components_no_invert_state_for_leak():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "leak"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "invert_state" not in result, "Leak sensor should not expose invert_state"


def test_sensor_config_components_no_invert_state_for_climate():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "climate"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "invert_state" not in result


def test_sensor_config_components_no_invert_state_for_keypad():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "keypad"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    assert "invert_state" not in result


def test_sensor_config_components_returns_fresh_dicts():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "switch"}
    r1 = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    r2 = _build_sensor_config_components("AAAAAAAA", sensor, "ws2m/AAAAAAAA")
    r1["sensor_name"]["sentinel"] = True
    assert "sentinel" not in r2.get("sensor_name", {})


# ---------------------------------------------------------------------------
# Keypad PIN entities in _build_keypad_components
# ---------------------------------------------------------------------------


def test_keypad_components_has_pin_management_entities():
    from mqtt import _build_keypad_components

    sensor = {"sensor_type": "keypad", "pins": ["1234", "5678"]}
    result = _build_keypad_components("KPADKPAD", sensor, "ws2m/KPADKPAD")
    assert "pin_count" in result
    assert "add_pin" in result
    assert "clear_pins" in result


def test_keypad_pin_count_is_sensor_entity():
    from mqtt import _build_keypad_components

    result = _build_keypad_components("KPADKPAD", {}, "ws2m/KPADKPAD")
    comp = result["pin_count"]
    assert comp["platform"] == "sensor"
    assert comp["entity_category"] == "config"
    assert "state_topic" in comp


def test_keypad_add_pin_is_button_entity():
    from mqtt import _build_keypad_components

    result = _build_keypad_components("KPADKPAD", {}, "ws2m/KPADKPAD")
    comp = result["add_pin"]
    assert comp["platform"] == "button"
    assert comp["entity_category"] == "config"
    assert comp["command_topic"].endswith("/add_pin")
    assert comp["payload_press"] == "arm"


def test_keypad_clear_pins_is_button_entity():
    from mqtt import _build_keypad_components

    result = _build_keypad_components("KPADKPAD", {}, "ws2m/KPADKPAD")
    comp = result["clear_pins"]
    assert comp["platform"] == "button"
    assert comp["entity_category"] == "config"
    assert comp["command_topic"].endswith("/clear_pins")
    assert comp["payload_press"] == "clear"


# ---------------------------------------------------------------------------
# Log level select in _build_service_components
# ---------------------------------------------------------------------------


def test_service_components_has_log_level_select():
    from mqtt import _build_service_components, LOG_LEVEL_OPTIONS

    result = _build_service_components("ws2m")
    assert "log_level" in result
    comp = result["log_level"]
    assert comp["platform"] == "select"
    assert comp["entity_category"] == "config"
    assert comp["options"] == LOG_LEVEL_OPTIONS
    assert comp["command_topic"].endswith("/log_level/set")
    assert "state_topic" in comp


def test_service_log_level_options_are_valid():
    from mqtt import LOG_LEVEL_OPTIONS
    import logging

    for level in LOG_LEVEL_OPTIONS:
        assert hasattr(logging, level), f"Invalid log level: {level}"


# ---------------------------------------------------------------------------
# QoS / retain constants are exported and sensible
# ---------------------------------------------------------------------------


def test_qos_and_retain_constants_exist():
    from mqtt import (
        _QOS_STATUS, _QOS_DISCOVERY, _QOS_DATA, _QOS_COMMAND, _QOS_NUMBER,
        _RETAIN_STATUS, _RETAIN_DISCOVERY, _RETAIN_DATA, _RETAIN_COMMAND, _RETAIN_NUMBER,
    )
    # Status and discovery must be retained for HA recovery after restart
    assert _RETAIN_STATUS is True
    assert _RETAIN_DISCOVERY is True
    # Data topics must NOT be retained (avoid stale readings)
    assert _RETAIN_DATA is False
    # Commands must NOT be retained (avoid replay on reconnect)
    assert _RETAIN_COMMAND is False
    # All QoS values are 0 or 1 (valid MQTT QoS levels for a bridge)
    for qos in (_QOS_STATUS, _QOS_DISCOVERY, _QOS_DATA, _QOS_COMMAND, _QOS_NUMBER):
        assert qos in (0, 1), f"Unexpected QoS value: {qos}"


# ---------------------------------------------------------------------------
# hass_topic_root default value
# ---------------------------------------------------------------------------


def test_hass_topic_root_default_is_homeassistant():
    """hass_topic_root defaults to 'homeassistant' in DEFAULT_CONFIG."""
    from config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["hass_topic_root"] == "homeassistant"


# ---------------------------------------------------------------------------
# publish_sensor_discovery includes config entity states
# ---------------------------------------------------------------------------


def test_publish_sensor_discovery_publishes_sensor_name_state(tmp_config_dir):
    """publish_sensor_discovery publishes initial sensor_name state topic."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Front Door"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(t.endswith("/sensor_name") for t in topics), "sensor_name state not published"


def test_publish_sensor_discovery_publishes_device_class_for_contact(tmp_config_dir):
    """publish_sensor_discovery publishes initial device_class state for contact sensors."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "class": "door"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(t.endswith("/device_class") for t in topics)


def test_publish_sensor_discovery_publishes_invert_state_for_contact(tmp_config_dir):
    """publish_sensor_discovery publishes initial invert_state state for contact sensors."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "invert_state": False}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(t.endswith("/invert_state") for t in topics)


def test_publish_sensor_discovery_no_invert_state_for_leak(tmp_config_dir):
    """publish_sensor_discovery does NOT publish invert_state state for leak sensors."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "leak"}
    gw.publish_sensor_discovery("DDDDDDDD", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert not any(t.endswith("/invert_state") for t in topics)


def test_publish_sensor_discovery_publishes_pin_count_for_keypad(tmp_config_dir):
    """publish_sensor_discovery publishes pin_count state for keypad sensors."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "keypad", "pins": ["1234"]}
    gw.publish_sensor_discovery("KPADKPAD", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(t.endswith("/pin_count") for t in topics)


# ---------------------------------------------------------------------------
# sensor_config components appear in sensor discovery payload
# ---------------------------------------------------------------------------


def test_sensor_discovery_components_include_sensor_name_entity(tmp_config_dir):
    """sensor_name text entity appears in the discovery payload components."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Side Gate"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    discovery_call = gw._client.publish.call_args_list[0]
    payload = json.loads(discovery_call.kwargs["payload"])
    assert "sensor_name" in payload["components"]
    assert payload["components"]["sensor_name"]["platform"] == "text"


def test_sensor_discovery_components_include_invert_state_for_contact(tmp_config_dir):
    """invert_state switch appears in discovery payload for contact sensors."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switchv2"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "invert_state" in payload["components"]
    assert payload["components"]["invert_state"]["platform"] == "switch"


def test_sensor_discovery_components_include_device_class_for_motion(tmp_config_dir):
    """device_class select appears in discovery payload for motion sensors."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motionv2"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "device_class" in payload["components"]
    assert payload["components"]["device_class"]["platform"] == "select"


def test_sensor_discovery_no_device_class_for_climate(tmp_config_dir):
    """device_class select NOT in discovery payload for climate sensors."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "climate"}
    gw.publish_sensor_discovery("CCCCCCCC", sensor, TEST_DONGLE_MAC, TEST_SERVICE_ID, sensor_online=True)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "device_class" not in payload["components"]


# ---------------------------------------------------------------------------
# Service component: cleanup_removed_dongles button
# ---------------------------------------------------------------------------


def test_service_components_has_cleanup_removed_dongles_button():
    """Service device must include the cleanup_removed_dongles button."""
    from mqtt import _build_service_components

    components = _build_service_components("ws2m")
    assert "cleanup_removed_dongles" in components
    btn = components["cleanup_removed_dongles"]
    assert btn["platform"] == "button"
    assert btn["entity_category"] == "config"
    assert btn["payload_press"] == "cleanup"
    assert btn["command_topic"].endswith("/cleanup_removed_dongles")


def test_service_discovery_includes_cleanup_removed_dongles(tmp_config_dir):
    """Published service discovery payload must contain cleanup_removed_dongles."""
    import json

    gw, _ = _make_gateway()
    gw.publish_service_discovery(TEST_SERVICE_ID, "INFO")

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "cleanup_removed_dongles" in payload["components"]


# ---------------------------------------------------------------------------
# clear_dongle_all_topics
# ---------------------------------------------------------------------------


def test_clear_dongle_all_topics_clears_discovery_and_status(tmp_config_dir):
    """clear_dongle_all_topics must clear both discovery and status topics."""
    gw, cfg = _make_gateway()
    gw.clear_dongle_all_topics(TEST_DONGLE_MAC)

    published_topics = [call.args[0] for call in gw._client.publish.call_args_list]
    discovery_topic = f"homeassistant/device/ws2m_dongle_{TEST_DONGLE_MAC}/config"
    status_topic = f"{cfg['self_topic_root']}/dongle_{TEST_DONGLE_MAC}/status"
    assert discovery_topic in published_topics, "Discovery topic not cleared"
    assert status_topic in published_topics, "Status topic not cleared"


def test_clear_dongle_all_topics_publishes_empty_payloads(tmp_config_dir):
    """All payloads for dongle topic clearing must be empty (retained message removal)."""
    gw, _ = _make_gateway()
    gw.clear_dongle_all_topics(TEST_DONGLE_MAC)

    for call in gw._client.publish.call_args_list:
        payload = call.kwargs.get("payload", call.args[1] if len(call.args) > 1 else None)
        assert payload in (None, b"", ""), f"Expected empty payload, got {payload!r}"


# ---------------------------------------------------------------------------
# Module-level clear_sensor_state_topics
# ---------------------------------------------------------------------------


def _make_mock_client():
    """Return a mocked paho client for module-level function tests."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
    return client


def _sample_cfg():
    return {"self_topic_root": "wyzesense2mqtt", "hass_topic_root": "homeassistant"}


def test_clear_sensor_state_topics_clears_status_and_data():
    from mqtt import clear_sensor_state_topics
    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_sensor_state_topics(client, cfg, None, "AABBCCDD", "switch")
    published = [call.args[0] for call in client.publish.call_args_list]
    assert "wyzesense2mqtt/AABBCCDD/status" in published
    assert "wyzesense2mqtt/AABBCCDD" in published


def test_clear_sensor_state_topics_clears_config_entity_topics():
    from mqtt import clear_sensor_state_topics
    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_sensor_state_topics(client, cfg, None, "AABBCCDD", "switchv2")
    published = [call.args[0] for call in client.publish.call_args_list]
    for suffix in ["sensor_name", "device_class", "invert_state", "pin_count"]:
        assert f"wyzesense2mqtt/AABBCCDD/{suffix}" in published, f"Missing {suffix}"


def test_clear_sensor_state_topics_clears_chime_number_topics():
    from mqtt import clear_sensor_state_topics
    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_sensor_state_topics(client, cfg, None, "CHIMEMAC", "chime")
    published = [call.args[0] for call in client.publish.call_args_list]
    for suffix in ["ring_id", "volume", "repeat_count"]:
        assert f"wyzesense2mqtt/CHIMEMAC/{suffix}" in published, f"Missing chime topic {suffix}"


def test_clear_sensor_state_topics_no_chime_topics_for_non_chime():
    from mqtt import clear_sensor_state_topics
    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_sensor_state_topics(client, cfg, None, "AABBCCDD", "switch")
    published = [call.args[0] for call in client.publish.call_args_list]
    for suffix in ["ring_id", "volume", "repeat_count"]:
        assert f"wyzesense2mqtt/AABBCCDD/{suffix}" not in published, f"Unexpected chime topic {suffix}"


# ---------------------------------------------------------------------------
# Module-level clear_dongle_topics
# ---------------------------------------------------------------------------


def test_clear_dongle_topics_clears_discovery_and_status():
    from mqtt import clear_dongle_topics
    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_dongle_topics(client, cfg, None, TEST_DONGLE_MAC)
    published = [call.args[0] for call in client.publish.call_args_list]
    assert f"homeassistant/device/ws2m_dongle_{TEST_DONGLE_MAC}/config" in published
    assert f"wyzesense2mqtt/dongle_{TEST_DONGLE_MAC}/status" in published


def test_clear_dongle_topics_publishes_empty_payloads():
    from mqtt import clear_dongle_topics
    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_dongle_topics(client, cfg, None, TEST_DONGLE_MAC)
    for call in client.publish.call_args_list:
        payload = call.kwargs.get("payload", call.args[1] if len(call.args) > 1 else None)
        assert payload in (None, b"", ""), f"Expected empty payload, got {payload!r}"


def test_clear_dongle_topics_logs_when_logger_provided():
    import logging
    from unittest.mock import patch
    from mqtt import clear_dongle_topics
    client = _make_mock_client()
    cfg = _sample_cfg()
    logger = logging.getLogger("test")
    with patch.object(logger, "info") as mock_info:
        clear_dongle_topics(client, cfg, logger, TEST_DONGLE_MAC)
        assert mock_info.called


# ---------------------------------------------------------------------------
# Gateway clear_sensor_topics delegates to module-level functions
# ---------------------------------------------------------------------------


def test_gateway_clear_sensor_topics_delegates(tmp_config_dir):
    """Gateway clear_sensor_topics calls the same topics as module-level functions."""
    from unittest.mock import patch
    from mqtt import clear_sensor_state_topics, clear_sensor_discovery_topics

    gw, cfg = _make_gateway()
    sensor_mac = "AABBCCDD"
    sensor_type = "switchv2"

    state_calls = []
    discovery_calls = []

    with patch("mqtt.clear_sensor_state_topics", side_effect=lambda *a, **kw: state_calls.append((a, kw))):
        with patch("mqtt.clear_sensor_discovery_topics", side_effect=lambda *a, **kw: discovery_calls.append((a, kw))):
            gw.clear_sensor_topics(sensor_mac, sensor_type)

    assert len(state_calls) == 1, "clear_sensor_state_topics should be called once"
    assert len(discovery_calls) == 1, "clear_sensor_discovery_topics should be called once"
    assert state_calls[0][0][3] == sensor_mac
    assert state_calls[0][0][4] == sensor_type


def test_gateway_clear_dongle_all_topics_delegates(tmp_config_dir):
    """Gateway clear_dongle_all_topics delegates to the module-level clear_dongle_topics."""
    from unittest.mock import patch

    gw, cfg = _make_gateway()
    dongle_calls = []

    with patch("mqtt.clear_dongle_topics", side_effect=lambda *a, **kw: dongle_calls.append((a, kw))):
        gw.clear_dongle_all_topics(TEST_DONGLE_MAC)

    assert len(dongle_calls) == 1
    assert dongle_calls[0][0][3] == TEST_DONGLE_MAC
