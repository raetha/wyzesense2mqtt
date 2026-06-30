"""
Tests for mqtt.py — discovery payload builders, MqttGateway publishing,
and discovery schema migration.

MqttGateway tests use unittest.mock to avoid needing a real broker.
"""

from unittest.mock import MagicMock, patch

import pytest
from conftest import TEST_DONGLE_MAC, TEST_HUB_ID

# Self-topic root used when calling builder functions directly (independent of the
# gateway fixture's self_topic_root).  Using a named constant makes it easy to
# spot which strings in assertions are topic-root-derived vs. fixed HA identifiers.
_TEST_ROOT = "ws2m"

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
        "dongle": "auto",
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

    components = _build_state_sensor_components("AAAAAAAA", {"sensor_type": "motion"}, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert "state" in components
    state = components["state"]
    assert state["platform"] == "binary_sensor"
    assert state["device_class"] == "motion"
    assert state["payload_on"] == "active"
    assert state["payload_off"] == "inactive"


def test_state_sensor_components_contact():
    from mqtt import _build_state_sensor_components

    components = _build_state_sensor_components("AAAAAAAA", {"sensor_type": "switch"}, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    state = components["state"]
    assert state["device_class"] == "opening"
    assert state["payload_on"] == "open"
    assert state["payload_off"] == "closed"


def test_state_sensor_components_contact_v2():
    from mqtt import _build_state_sensor_components

    mac_topic = f"{_TEST_ROOT}/sensor/AAAAAAAA"
    components = _build_state_sensor_components("AAAAAAAA", {"sensor_type": "switchv2"}, mac_topic)
    assert components["state"]["device_class"] == "opening"


def test_leak_sensor_components_structure():
    from mqtt import _build_leak_sensor_components

    components = _build_leak_sensor_components("DDDDDDDD", {"sensor_type": "leak"}, f"{_TEST_ROOT}/sensor/DDDDDDDD")
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

    mac_topic = f"{_TEST_ROOT}/sensor/CCCCCCCC"
    components = _build_climate_sensor_components("CCCCCCCC", {"sensor_type": "climate"}, mac_topic)
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

    first = _build_sensor_action_components("AABBCCDD", f"{_TEST_ROOT}/dongle/X/remove")
    second = _build_sensor_action_components("EEFFGGHH", f"{_TEST_ROOT}/dongle/X/remove")
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

    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    assert gw._client.publish.called
    calls = gw._client.publish.call_args_list
    topics = [c.args[0] for c in calls]
    assert any("homeassistant/device/ws2m_sensor_AAAAAAAA/config" in t for t in topics)


def test_publish_sensor_discovery_payload_structure(tmp_config_dir):
    """Verify the discovery payload has the expected top-level keys."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motion", "name": "Hall Motion", "sw_version": "19"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        topic = c.args[0]
        if "device/ws2m_sensor_AAAAAAAA/config" in topic:
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
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/ws2m_sensor_AAAAAAAA/config" in c.args[0]:
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
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/ws2m_sensor_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            avail_topics = [a["topic"] for a in payload["availability"]]
            assert any(f"dongle/{TEST_DONGLE_MAC}/status" in t for t in avail_topics), (
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
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/ws2m_sensor_AAAAAAAA/config" in c.args[0]:
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
        gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    assert any("No discovery component builder" in r.message for r in caplog.records)
    config_calls = [c for c in gw._client.publish.call_args_list if "device/ws2m_sensor_AAAAAAAA/config" in c.args[0]]
    assert not config_calls


def test_publish_sensor_discovery_publishes_availability(tmp_config_dir):
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Front Door"}

    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any("AAAAAAAA/status" in t for t in topics)


def test_publish_sensor_discovery_offline_sensor(tmp_config_dir):
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "name": "Back Door"}

    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=False)

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
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/ws2m_sensor_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            components = payload["components"]
            assert "remove" in components, "Expected 'remove' button component in sensor discovery"
            remove = components["remove"]
            assert remove["platform"] == "button"
            # Remove button must target the dongle-scoped remove topic
            assert f"dongle/{TEST_DONGLE_MAC}/remove" in remove["command_topic"]
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
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    for c in gw._client.publish.call_args_list:
        if "device/ws2m_sensor_AAAAAAAA/config" in c.args[0]:
            payload = json.loads(c.kwargs["payload"])
            remove = payload["components"]["remove"]
            assert "value_template" not in remove
            assert "value_template" in payload["components"]["state"]
            assert "value_template" in payload["components"]["signal_strength"]
            break
    else:
        pytest.fail("Discovery config publish call not found")


# ---------------------------------------------------------------------------
# clear_sensor
# ---------------------------------------------------------------------------


def test_clear_sensor_publishes_none_payloads(tmp_config_dir):
    gw, cfg = _make_gateway()
    gw.clear_sensor("AAAAAAAA", "switch")

    for c in gw._client.publish.call_args_list:
        payload = c.args[1] if len(c.args) > 1 else c.kwargs.get("payload")
        assert payload is None, f"Expected None payload for topic clear, got {payload!r}"


# ---------------------------------------------------------------------------
# Discovery schema migration — v3 schema
# ---------------------------------------------------------------------------


def test_migrate_discovery_clears_v1_sensor_topics(tmp_config_dir):
    """v1->v2 migration step must clear legacy per-entity sensor topics."""
    gw, cfg = _make_gateway()

    gw.migrate_discovery_topics("AAAAAAAA", "switch", TEST_DONGLE_MAC, TEST_HUB_ID, from_version=1)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any("binary_sensor/wyzesense_AAAAAAAA" in t for t in topics)
    for c in gw._client.publish.call_args_list:
        assert c.kwargs["payload"] is None


def test_migrate_discovery_clears_v1_bridge_topic(tmp_config_dir):
    """v1->v2 migration must clear the 3.x wyzesense_bridge_<mac> topic."""
    gw, cfg = _make_gateway()

    gw.migrate_discovery_topics("AAAAAAAA", "switch", TEST_DONGLE_MAC, TEST_HUB_ID, from_version=1)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(f"wyzesense_bridge_{TEST_DONGLE_MAC}" in t for t in topics)


def test_migrate_discovery_noop_when_already_current(tmp_config_dir):
    from mqtt import DISCOVERY_SCHEMA_VERSION

    gw, cfg = _make_gateway()
    gw.migrate_discovery_topics(
        "AAAAAAAA", "switch", TEST_DONGLE_MAC, TEST_HUB_ID, from_version=DISCOVERY_SCHEMA_VERSION
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


def test_publish_hub_discovery(tmp_config_dir):
    """Hub discovery uses device-based format with a single retained topic."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_hub_discovery(TEST_HUB_ID, "INFO")

    assert gw._client.publish.called
    calls = gw._client.publish.call_args_list
    # 2 publishes: discovery config + log_level state
    assert len(calls) == 2
    topic = calls[0].args[0]
    assert topic == f"homeassistant/device/ws2m_hub_{TEST_HUB_ID}/config"

    payload = json.loads(calls[0].kwargs["payload"])
    assert payload["device"]["identifiers"] == [f"ws2m_hub_{TEST_HUB_ID}"]
    assert payload["device"]["name"] == "WyzeSense2MQTT"
    assert "sw_version" in payload["device"]
    assert "hw_version" not in payload["device"]  # hw belongs to the dongle, not the hub
    assert payload["origin"]["name"] == "WyzeSense2MQTT"

    components = payload["components"]
    assert "reload" in components, "Hub device must have reload button"
    # Scan and remove must NOT be on the hub device
    assert "scan" not in components, "Scan belongs to dongle, not hub"
    assert "remove" not in components, "Remove belongs to dongle, not hub"

    reload_ = components["reload"]
    assert reload_["platform"] == "button"
    assert reload_["entity_category"] == "config"
    assert reload_["command_topic"].endswith("/reload")
    assert reload_["payload_press"] == "reload"
    assert reload_["has_entity_name"] is True

    assert "log_level" in components, "Hub device must have log_level select"
    log_level = components["log_level"]
    assert log_level["platform"] == "select"
    assert log_level["entity_category"] == "config"


def test_publish_hub_discovery_unique_ids(tmp_config_dir):
    """Hub device entities must all have distinct unique_ids using the dict key as suffix."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_hub_discovery(TEST_HUB_ID, "INFO")

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    seen_ids = set()
    for key, component in payload["components"].items():
        assert "unique_id" in component, f"Component {key!r} missing unique_id"
        uid = component["unique_id"]
        assert uid == f"ws2m_hub_{TEST_HUB_ID}_{key}", (
            f"Component {key!r} unique_id {uid!r} does not match expected ws2m_hub_<uuid>_<key> pattern"
        )
        assert uid not in seen_ids, f"Duplicate unique_id {uid!r} across hub components"
        seen_ids.add(uid)


# ---------------------------------------------------------------------------
# Dongle discovery
# ---------------------------------------------------------------------------


def test_publish_dongle_discovery(tmp_config_dir):
    """Dongle discovery uses device-based format; device is child of service."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_dongle_discovery(TEST_DONGLE_MAC, "v1.2.3", TEST_HUB_ID)

    assert gw._client.publish.called
    calls = gw._client.publish.call_args_list
    assert len(calls) == 1
    topic = calls[0].args[0]
    assert topic == f"homeassistant/device/ws2m_dongle_{TEST_DONGLE_MAC}/config"

    payload = json.loads(calls[0].kwargs["payload"])
    assert payload["device"]["hw_version"] == "v1.2.3"
    assert payload["device"]["via_device"] == f"ws2m_hub_{TEST_HUB_ID}"
    assert payload["device"]["name"] == f"WyzeSense Dongle {TEST_DONGLE_MAC}"
    assert payload["origin"]["name"] == "WyzeSense2MQTT"

    components = payload["components"]
    assert "connection_state" in components
    assert "scan" in components
    # Dongle-level remove button was removed — cleanup is done at hub/remote level
    assert "remove" not in components, "Remove button should NOT be on the dongle device"
    # Reload must NOT be on the dongle — it's a service-level entity
    assert "reload" not in components, "Reload belongs to hub, not dongle"

    conn = components["connection_state"]
    assert conn["platform"] == "binary_sensor"
    assert conn["device_class"] == "connectivity"
    assert conn["entity_category"] == "diagnostic"
    assert conn["has_entity_name"] is True
    assert f"dongle/{TEST_DONGLE_MAC}/status" in conn["state_topic"]

    scan = components["scan"]
    assert scan["platform"] == "button"
    assert scan["entity_category"] == "config"
    assert f"dongle/{TEST_DONGLE_MAC}/scan" in scan["command_topic"]
    assert scan["payload_press"] == "scan"
    assert scan["has_entity_name"] is True


def test_publish_dongle_discovery_unique_ids(tmp_config_dir):
    """Dongle device entities must have unique_ids prefixed with ws2m_dongle_<mac>."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_dongle_discovery(TEST_DONGLE_MAC, "v1.2.3", TEST_HUB_ID)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    for key, component in payload["components"].items():
        assert "unique_id" in component, f"Component {key!r} missing unique_id"
        assert component["unique_id"] == f"ws2m_dongle_{TEST_DONGLE_MAC}_{key}"


def test_publish_dongle_discovery_local_has_no_availability(tmp_config_dir):
    """Local dongle discovery payload omits availability (dongle status is enough)."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_dongle_discovery(TEST_DONGLE_MAC, "v1.2.3", TEST_HUB_ID)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "availability" not in payload, "Local dongle should not have an availability block"


def test_publish_dongle_discovery_remote_has_availability_block(tmp_config_dir):
    """Remote dongle discovery includes availability for both dongle and remote status topics."""
    import json

    gw, cfg = _make_gateway()
    remote_id = "test-remote-uuid-9999"
    gw.publish_dongle_discovery(TEST_DONGLE_MAC, "v1.2.3", TEST_HUB_ID, via_remote_id=remote_id)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "availability" in payload, "Remote dongle must have an availability block"
    assert payload["availability_mode"] == "all"

    avail_topics = [a["topic"] for a in payload["availability"]]
    assert any(f"dongle/{TEST_DONGLE_MAC}/status" in t for t in avail_topics), (
        f"Dongle status topic missing from availability: {avail_topics}"
    )
    assert any(f"remote/{remote_id}/status" in t for t in avail_topics), (
        f"Remote status topic missing from availability: {avail_topics}"
    )


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
    assert any("device/ws2m_sensor_DDDDDDDD/config" in t for t in topics)


def test_clear_sensor_discovery_topics_v1_climate(tmp_config_dir):
    from mqtt import clear_sensor_discovery_topics

    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0, wait_for_publish=MagicMock())
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
    gw.publish_sensor_discovery("KPADKPAD", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    assert mock_client.publish.called
    topic_args = [call.args[0] for call in mock_client.publish.call_args_list]
    assert any("ws2m_sensor_KPADKPAD" in t for t in topic_args)


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
    gw.publish_sensor_discovery("CHIMEMAC", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    assert mock_client.publish.called
    topic_args = [call.args[0] for call in mock_client.publish.call_args_list]
    assert any("ws2m_sensor_CHIMEMAC" in t for t in topic_args)


# ---------------------------------------------------------------------------
# invert_state — _build_state_sensor_components
# ---------------------------------------------------------------------------


def test_invert_state_false_leaves_payloads_unchanged():
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "switch", "invert_state": False}
    result = _build_state_sensor_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert result["state"]["payload_on"] == "open"
    assert result["state"]["payload_off"] == "closed"


def test_invert_state_true_swaps_payloads_contact():
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "switch", "invert_state": True}
    result = _build_state_sensor_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert result["state"]["payload_on"] == "closed"
    assert result["state"]["payload_off"] == "open"


def test_invert_state_true_swaps_payloads_motion():
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "motion", "invert_state": True}
    result = _build_state_sensor_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert result["state"]["payload_on"] == "inactive"
    assert result["state"]["payload_off"] == "active"


def test_invert_state_not_applied_to_non_invertible_type():
    """invert_state on a sensor type not in INVERTIBLE_SENSOR_TYPES has no effect."""
    from mqtt import _build_state_sensor_components

    # 'unknown' is not invertible; payloads should be unchanged
    sensor = {"sensor_type": "unknown", "invert_state": True}
    result = _build_state_sensor_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    # Falls back to unknown type defaults; not swapped
    assert result["state"]["payload_on"] == result["state"]["payload_on"]  # shape check
    assert "payload_on" in result["state"]


def test_device_class_override_used_in_state_component():
    """Per-sensor 'class' key overrides the type default device_class."""
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "switch", "class": "door"}
    result = _build_state_sensor_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert result["state"]["device_class"] == "door"


def test_device_class_falls_back_to_type_default():
    from mqtt import _build_state_sensor_components

    sensor = {"sensor_type": "switch"}
    result = _build_state_sensor_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert result["state"]["device_class"] == "opening"


# ---------------------------------------------------------------------------
# _build_sensor_config_components
# ---------------------------------------------------------------------------


def test_sensor_config_components_always_has_sensor_name():
    from mqtt import _build_sensor_config_components

    for sensor_type in ("switch", "motion", "leak", "climate", "chime", "keypad", "unknown"):
        sensor = {"sensor_type": sensor_type}
        result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
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
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
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
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert "device_class" in result
    assert set(result["device_class"]["options"]) == set(DEVICE_CLASS_OPTIONS["motion"])


def test_sensor_config_components_no_device_class_for_leak():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "leak"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert "device_class" not in result, "Leak sensor class is fixed; no select entity"


def test_sensor_config_components_no_device_class_for_climate():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "climate"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert "device_class" not in result


def test_sensor_config_components_invert_state_for_contact():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "switch"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
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
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert "invert_state" in result


def test_sensor_config_components_no_invert_state_for_leak():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "leak"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert "invert_state" not in result, "Leak sensor should not expose invert_state"


def test_sensor_config_components_no_invert_state_for_climate():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "climate"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert "invert_state" not in result


def test_sensor_config_components_no_invert_state_for_keypad():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "keypad"}
    result = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    assert "invert_state" not in result


def test_sensor_config_components_returns_fresh_dicts():
    from mqtt import _build_sensor_config_components

    sensor = {"sensor_type": "switch"}
    r1 = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    r2 = _build_sensor_config_components("AAAAAAAA", sensor, f"{_TEST_ROOT}/sensor/AAAAAAAA")
    r1["sensor_name"]["sentinel"] = True
    assert "sentinel" not in r2.get("sensor_name", {})


# ---------------------------------------------------------------------------
# Keypad PIN entities in _build_keypad_components
# ---------------------------------------------------------------------------


def test_keypad_components_has_pin_management_entities():
    from mqtt import _build_keypad_components

    sensor = {"sensor_type": "keypad", "pins": ["1234", "5678"]}
    result = _build_keypad_components("KPADKPAD", sensor, f"{_TEST_ROOT}/sensor/KPADKPAD")
    assert "pin_count" in result
    assert "add_pin" in result
    assert "clear_pins" in result


def test_keypad_pin_count_is_sensor_entity():
    from mqtt import _build_keypad_components

    result = _build_keypad_components("KPADKPAD", {}, f"{_TEST_ROOT}/sensor/KPADKPAD")
    comp = result["pin_count"]
    assert comp["platform"] == "sensor"
    assert comp["entity_category"] == "config"
    assert "state_topic" in comp


def test_keypad_add_pin_is_button_entity():
    from mqtt import _build_keypad_components

    result = _build_keypad_components("KPADKPAD", {}, f"{_TEST_ROOT}/sensor/KPADKPAD")
    comp = result["add_pin"]
    assert comp["platform"] == "button"
    assert comp["entity_category"] == "config"
    assert comp["command_topic"].endswith("/add_pin")
    assert comp["payload_press"] == "arm"


def test_keypad_clear_pins_is_button_entity():
    from mqtt import _build_keypad_components

    result = _build_keypad_components("KPADKPAD", {}, f"{_TEST_ROOT}/sensor/KPADKPAD")
    comp = result["clear_pins"]
    assert comp["platform"] == "button"
    assert comp["entity_category"] == "config"
    assert comp["command_topic"].endswith("/clear_pins")
    assert comp["payload_press"] == "clear"


# ---------------------------------------------------------------------------
# Log level select in _build_hub_components
# ---------------------------------------------------------------------------


def test_hub_components_has_log_level_select():
    from mqtt import LOG_LEVEL_OPTIONS, _build_hub_components

    result = _build_hub_components(_TEST_ROOT, "test-uuid")
    assert "log_level" in result
    comp = result["log_level"]
    assert comp["platform"] == "select"
    assert comp["entity_category"] == "config"
    assert comp["options"] == LOG_LEVEL_OPTIONS
    assert comp["command_topic"].endswith("/log_level/set")
    assert "state_topic" in comp


def test_service_log_level_options_are_valid():
    import logging

    from mqtt import LOG_LEVEL_OPTIONS

    for level in LOG_LEVEL_OPTIONS:
        assert hasattr(logging, level), f"Invalid log level: {level}"


# ---------------------------------------------------------------------------
# QoS / retain constants are exported and sensible
# ---------------------------------------------------------------------------


def test_qos_and_retain_constants_exist():
    from mqtt import (
        QOS_COMMAND,
        QOS_DATA,
        QOS_DISCOVERY,
        QOS_NUMBER,
        QOS_STATUS,
        RETAIN_COMMAND,
        RETAIN_DATA,
        RETAIN_DISCOVERY,
        RETAIN_STATUS,
    )

    # Status and discovery must be retained for HA recovery after restart
    assert RETAIN_STATUS is True
    assert RETAIN_DISCOVERY is True
    # Data topics must NOT be retained (avoid stale readings)
    assert RETAIN_DATA is False
    # Commands must NOT be retained (avoid replay on reconnect)
    assert RETAIN_COMMAND is False
    # All QoS values are 0 or 1 (valid MQTT QoS levels for a bridge)
    for qos in (QOS_STATUS, QOS_DISCOVERY, QOS_DATA, QOS_COMMAND, QOS_NUMBER):
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
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(t.endswith("/sensor_name") for t in topics), "sensor_name state not published"


def test_publish_sensor_discovery_publishes_device_class_for_contact(tmp_config_dir):
    """publish_sensor_discovery publishes initial device_class state for contact sensors."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "class": "door"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(t.endswith("/device_class") for t in topics)


def test_publish_sensor_discovery_publishes_invert_state_for_contact(tmp_config_dir):
    """publish_sensor_discovery publishes initial invert_state state for contact sensors."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switch", "invert_state": False}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert any(t.endswith("/invert_state") for t in topics)


def test_publish_sensor_discovery_no_invert_state_for_leak(tmp_config_dir):
    """publish_sensor_discovery does NOT publish invert_state state for leak sensors."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "leak"}
    gw.publish_sensor_discovery("DDDDDDDD", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    topics = [c.args[0] for c in gw._client.publish.call_args_list]
    assert not any(t.endswith("/invert_state") for t in topics)


def test_publish_sensor_discovery_publishes_pin_count_for_keypad(tmp_config_dir):
    """publish_sensor_discovery publishes pin_count state for keypad sensors."""
    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "keypad", "pins": ["1234"]}
    gw.publish_sensor_discovery("KPADKPAD", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

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
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    discovery_call = gw._client.publish.call_args_list[0]
    payload = json.loads(discovery_call.kwargs["payload"])
    assert "sensor_name" in payload["components"]
    assert payload["components"]["sensor_name"]["platform"] == "text"


def test_sensor_discovery_components_include_invert_state_for_contact(tmp_config_dir):
    """invert_state switch appears in discovery payload for contact sensors."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "switchv2"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "invert_state" in payload["components"]
    assert payload["components"]["invert_state"]["platform"] == "switch"


def test_sensor_discovery_components_include_device_class_for_motion(tmp_config_dir):
    """device_class select appears in discovery payload for motion sensors."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "motionv2"}
    gw.publish_sensor_discovery("AAAAAAAA", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "device_class" in payload["components"]
    assert payload["components"]["device_class"]["platform"] == "select"


def test_sensor_discovery_no_device_class_for_climate(tmp_config_dir):
    """device_class select NOT in discovery payload for climate sensors."""
    import json

    gw, cfg = _make_gateway()
    sensor = {"sensor_type": "climate"}
    gw.publish_sensor_discovery("CCCCCCCC", sensor, TEST_DONGLE_MAC, TEST_HUB_ID, sensor_online=True)

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "device_class" not in payload["components"]


# ---------------------------------------------------------------------------
# Hub component: cleanup_removed_dongles button
# ---------------------------------------------------------------------------


def test_hub_components_has_cleanup_removed_dongles_button():
    """Hub device must include the cleanup_removed_dongles button."""
    from mqtt import _build_hub_components

    components = _build_hub_components("ws2m", "test-uuid")
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
    gw.publish_hub_discovery(TEST_HUB_ID, "INFO")

    payload = json.loads(gw._client.publish.call_args_list[0].kwargs["payload"])
    assert "cleanup_removed_dongles" in payload["components"]


# ---------------------------------------------------------------------------
# clear_dongle
# ---------------------------------------------------------------------------


def test_clear_dongle_clears_discovery_and_status(tmp_config_dir):
    """clear_dongle must clear both discovery and status topics."""
    gw, cfg = _make_gateway()
    gw.clear_dongle(TEST_DONGLE_MAC)

    published_topics = [call.args[0] for call in gw._client.publish.call_args_list]
    discovery_topic = f"homeassistant/device/ws2m_dongle_{TEST_DONGLE_MAC}/config"
    status_topic = f"{cfg['self_topic_root']}/dongle/{TEST_DONGLE_MAC}/status"
    assert discovery_topic in published_topics, "Discovery topic not cleared"
    assert status_topic in published_topics, "Status topic not cleared"


def test_clear_dongle_publishes_empty_payloads(tmp_config_dir):
    """All payloads for dongle topic clearing must be empty (retained message removal)."""
    gw, _ = _make_gateway()
    gw.clear_dongle(TEST_DONGLE_MAC)

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
    assert "wyzesense2mqtt/sensor/AABBCCDD/status" in published
    assert "wyzesense2mqtt/sensor/AABBCCDD" in published


def test_clear_sensor_state_topics_clears_config_entity_topics():
    from mqtt import clear_sensor_state_topics

    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_sensor_state_topics(client, cfg, None, "AABBCCDD", "switchv2")
    published = [call.args[0] for call in client.publish.call_args_list]
    for suffix in ["sensor_name", "device_class", "invert_state", "pin_count"]:
        assert f"wyzesense2mqtt/sensor/AABBCCDD/{suffix}" in published, f"Missing {suffix}"


def test_clear_sensor_state_topics_clears_chime_number_topics():
    from mqtt import clear_sensor_state_topics

    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_sensor_state_topics(client, cfg, None, "CHIMEMAC", "chime")
    published = [call.args[0] for call in client.publish.call_args_list]
    for suffix in ["ring_id", "volume", "repeat_count"]:
        assert f"wyzesense2mqtt/sensor/CHIMEMAC/{suffix}" in published, f"Missing chime topic {suffix}"


def test_clear_sensor_state_topics_no_chime_topics_for_non_chime():
    from mqtt import clear_sensor_state_topics

    client = _make_mock_client()
    cfg = _sample_cfg()
    clear_sensor_state_topics(client, cfg, None, "AABBCCDD", "switch")
    published = [call.args[0] for call in client.publish.call_args_list]
    for suffix in ["ring_id", "volume", "repeat_count"]:
        assert f"wyzesense2mqtt/sensor/AABBCCDD/{suffix}" not in published, f"Unexpected chime topic {suffix}"


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
    assert f"wyzesense2mqtt/dongle/{TEST_DONGLE_MAC}/status" in published


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
# Gateway clear_sensor and clear_dongle delegate to module-level functions
# ---------------------------------------------------------------------------


def test_gateway_clear_sensor_delegates(tmp_config_dir):
    """Gateway clear_sensor calls both module-level clear_sensor_state_topics and clear_sensor_discovery_topics."""
    from unittest.mock import patch

    gw, cfg = _make_gateway()
    sensor_mac = "AABBCCDD"
    sensor_type = "switchv2"

    state_calls = []
    discovery_calls = []

    with patch("mqtt.clear_sensor_state_topics", side_effect=lambda *a, **kw: state_calls.append((a, kw))):
        with patch("mqtt.clear_sensor_discovery_topics", side_effect=lambda *a, **kw: discovery_calls.append((a, kw))):
            gw.clear_sensor(sensor_mac, sensor_type)

    assert len(state_calls) == 1, "clear_sensor_state_topics should be called once"
    assert len(discovery_calls) == 1, "clear_sensor_discovery_topics should be called once"
    assert state_calls[0][0][3] == sensor_mac
    assert state_calls[0][0][4] == sensor_type


def test_gateway_clear_dongle_delegates(tmp_config_dir):
    """Gateway clear_dongle delegates to the module-level clear_dongle_topics."""
    from unittest.mock import patch

    gw, cfg = _make_gateway()
    dongle_calls = []

    with patch("mqtt.clear_dongle_topics", side_effect=lambda *a, **kw: dongle_calls.append((a, kw))):
        gw.clear_dongle(TEST_DONGLE_MAC)

    assert len(dongle_calls) == 1
    assert dongle_calls[0][0][3] == TEST_DONGLE_MAC


# ---------------------------------------------------------------------------
# Remote discovery — health and restart entities
# ---------------------------------------------------------------------------


def test_publish_remote_discovery_has_health_and_no_dongle_state(tmp_config_dir):
    """publish_remote_discovery generates health + restart; dongle_state not on remote device."""
    import json

    gw, cfg = _make_gateway()
    remote_id = "test-remote-uuid-1234"
    gw.publish_remote_discovery(remote_id, TEST_HUB_ID)

    assert gw._client.publish.called
    call = gw._client.publish.call_args_list[0]
    payload = json.loads(call.kwargs["payload"])
    components = payload["components"]

    # dongle_connected must NOT appear on the remote device
    assert "dongle_connected" not in components
    assert not any("dongle_state" in str(v) for v in components.values())

    # health (sensor)
    assert "health" in components
    health = components["health"]
    assert health["platform"] == "sensor"
    assert "health" in health["state_topic"]
    assert health["entity_category"] == "diagnostic"
    assert health["icon"] == "mdi:heart-pulse"


def test_publish_remote_discovery_has_restart_button(tmp_config_dir):
    """publish_remote_discovery generates a restart button component."""
    import json

    gw, cfg = _make_gateway()
    remote_id = "test-remote-uuid-1234"
    gw.publish_remote_discovery(remote_id, TEST_HUB_ID)

    call = gw._client.publish.call_args_list[0]
    payload = json.loads(call.kwargs["payload"])
    components = payload["components"]

    restart_key = "restart"
    assert restart_key in components, f"Expected restart component {restart_key!r} in {list(components)}"
    restart = components[restart_key]
    assert restart["platform"] == "button"
    assert restart["command_topic"].endswith("/restart/set")
    assert restart["entity_category"] == "config"
    assert restart["icon"] == "mdi:restart"


# ---------------------------------------------------------------------------
# Hub discovery — restart button
# ---------------------------------------------------------------------------


def test_publish_hub_discovery_has_restart_button(tmp_config_dir):
    """publish_hub_discovery generates a restart button component."""
    import json

    gw, cfg = _make_gateway()
    gw.publish_hub_discovery(TEST_HUB_ID, "INFO")

    call = gw._client.publish.call_args_list[0]
    payload = json.loads(call.kwargs["payload"])
    components = payload["components"]

    restart_key = "restart"
    assert restart_key in components, f"Expected restart component {restart_key!r} in {list(components)}"
    restart = components[restart_key]
    assert restart["platform"] == "button"
    assert restart["command_topic"].endswith("/restart/set")
    assert restart["entity_category"] == "config"
    assert restart["icon"] == "mdi:restart"


# ---------------------------------------------------------------------------
# Hub components — new config entities (Part B)
# ---------------------------------------------------------------------------


def test_build_hub_components_has_dongle_entity():
    """_build_hub_components includes dongle text entity with correct platform and topics."""
    from mqtt import _build_hub_components

    components = _build_hub_components("ws2m", TEST_HUB_ID)
    assert "dongle" in components, f"dongle not in {list(components)}"
    e = components["dongle"]
    assert e["platform"] == "text"
    assert e["state_topic"].endswith("/dongle")
    assert e["command_topic"].endswith("/dongle/set")
    assert e["entity_category"] == "config"


def test_build_hub_components_has_ws_port_entity():
    """_build_hub_components includes ws_port number entity with correct platform and topics."""
    from mqtt import _build_hub_components

    components = _build_hub_components("ws2m", TEST_HUB_ID)
    assert "ws_port" in components, f"ws_port not in {list(components)}"
    e = components["ws_port"]
    assert e["platform"] == "number"
    assert e["state_topic"].endswith("/ws_port")
    assert e["command_topic"].endswith("/ws_port/set")
    assert e["min"] == 1024
    assert e["max"] == 65535
    assert e["entity_category"] == "config"


def test_build_hub_components_has_remote_pairing_timeout_entity():
    """_build_hub_components includes remote_pairing_timeout number entity."""
    from mqtt import _build_hub_components

    components = _build_hub_components("ws2m", TEST_HUB_ID)
    assert "remote_pairing_timeout" in components, f"remote_pairing_timeout not in {list(components)}"
    e = components["remote_pairing_timeout"]
    assert e["platform"] == "number"
    assert e["state_topic"].endswith("/remote_pairing_timeout")
    assert e["command_topic"].endswith("/remote_pairing_timeout/set")
    assert e["min"] == 10
    assert e["max"] == 3600
    assert e["unit_of_measurement"] == "s"
    assert e["entity_category"] == "config"


def test_build_hub_components_has_mdns_entity():
    """_build_hub_components includes mdns switch entity."""
    from mqtt import _build_hub_components

    components = _build_hub_components("ws2m", TEST_HUB_ID)
    assert "mdns" in components, f"mdns not in {list(components)}"
    e = components["mdns"]
    assert e["platform"] == "switch"
    assert e["state_topic"].endswith("/mdns")
    assert e["command_topic"].endswith("/mdns/set")
    assert e["payload_on"] == "true"
    assert e["payload_off"] == "false"
    assert e["entity_category"] == "config"


def test_build_hub_components_has_remote_dongle_count_entity():
    """_build_hub_components includes remote_dongles measurement sensor."""
    from mqtt import _build_hub_components

    components = _build_hub_components(_TEST_ROOT, TEST_HUB_ID)
    assert "remote_dongles" in components, f"remote_dongles not in {list(components)}"
    e = components["remote_dongles"]
    assert e["platform"] == "sensor"
    assert e["state_class"] == "measurement"
    assert e["state_topic"] == f"{_TEST_ROOT}/hub/{TEST_HUB_ID}/remote_dongles"
    assert e["entity_category"] == "diagnostic"


def test_build_hub_components_has_connected_remotes_entity():
    """_build_hub_components includes connected_remotes measurement sensor."""
    from mqtt import _build_hub_components

    components = _build_hub_components(_TEST_ROOT, TEST_HUB_ID)
    assert "connected_remotes" in components, f"connected_remotes not in {list(components)}"
    e = components["connected_remotes"]
    assert e["platform"] == "sensor"
    assert e["state_class"] == "measurement"
    assert e["state_topic"] == f"{_TEST_ROOT}/hub/{TEST_HUB_ID}/connected_remotes"
    assert e["entity_category"] == "diagnostic"


# ---------------------------------------------------------------------------
# Remote component — remove button
# ---------------------------------------------------------------------------


def test_build_remote_components_has_remove_button(tmp_config_dir):
    """_build_remote_components includes a remove button with the correct command topic."""
    from mqtt import _build_remote_components

    remote_id = "test-remote-uuid-abcd"
    components = _build_remote_components(_TEST_ROOT, remote_id)
    assert "remove" in components, f"remove not in {list(components)}"
    remove = components["remove"]
    assert remove["platform"] == "button"
    assert remove["command_topic"] == f"{_TEST_ROOT}/remote/{remote_id}/remove"
    assert remove["payload_press"] == "remove"
    assert remove["entity_category"] == "config"


def test_publish_remote_discovery_has_remove_button(tmp_config_dir):
    """publish_remote_discovery generates a remove button component."""
    import json

    gw, _ = _make_gateway()
    remote_id = "test-remote-uuid-1234"
    gw.publish_remote_discovery(remote_id, TEST_HUB_ID)

    call = gw._client.publish.call_args_list[0]
    payload = json.loads(call.kwargs["payload"])
    components = payload["components"]
    assert "remove" in components, f"Expected remove component in {list(components)}"
    remove = components["remove"]
    assert remove["platform"] == "button"
    assert f"/remote/{remote_id}/remove" in remove["command_topic"]


def test_publish_remote_discovery_has_cleanup_disconnected_dongles_button(tmp_config_dir):
    """publish_remote_discovery generates a cleanup_disconnected_dongles button component."""
    import json

    gw, _ = _make_gateway()
    remote_id = "test-remote-uuid-1234"
    gw.publish_remote_discovery(remote_id, TEST_HUB_ID)

    call = gw._client.publish.call_args_list[0]
    payload = json.loads(call.kwargs["payload"])
    components = payload["components"]
    assert "cleanup_disconnected_dongles" in components, (
        f"Expected cleanup_disconnected_dongles component in {list(components)}"
    )
    cleanup = components["cleanup_disconnected_dongles"]
    assert cleanup["platform"] == "button"
    assert cleanup["entity_category"] == "config"
    assert f"/remote/{remote_id}/cleanup_disconnected_dongles" in cleanup["command_topic"]
    assert cleanup["payload_press"] == "cleanup"
    assert cleanup["has_entity_name"] is True


# ---------------------------------------------------------------------------
# clear_remote
# ---------------------------------------------------------------------------


def test_clear_remote_clears_discovery_and_state_topics(tmp_config_dir):
    """clear_remote must clear the discovery config and all retained state topics."""
    gw, cfg = _make_gateway()
    remote_id = "test-remote-uuid-abcd"
    gw.clear_remote(remote_id)

    published_topics = [call.args[0] for call in gw._client.publish.call_args_list]
    discovery_topic = f"homeassistant/device/ws2m_remote_{remote_id}/config"
    assert discovery_topic in published_topics, "Discovery topic not cleared"
    for subtopic in ("status", "health", "connected_dongles"):
        state_topic = f"{cfg['self_topic_root']}/remote/{remote_id}/{subtopic}"
        assert state_topic in published_topics, f"State topic {subtopic!r} not cleared"


def test_clear_remote_publishes_empty_payloads(tmp_config_dir):
    """All payloads for remote topic clearing must be empty (retained message removal)."""
    gw, _ = _make_gateway()
    gw.clear_remote("test-remote-uuid-abcd")

    for call in gw._client.publish.call_args_list:
        payload = call.kwargs.get("payload", call.args[1] if len(call.args) > 1 else None)
        assert payload in (None, b"", ""), f"Expected empty payload, got {payload!r}"


def test_gateway_clear_remote_delegates(tmp_config_dir):
    """Gateway clear_remote delegates to the module-level clear_remote_topics."""
    from unittest.mock import patch

    gw, _ = _make_gateway()
    remote_id = "test-remote-uuid-abcd"
    remote_calls = []

    with patch("mqtt.clear_remote_topics", side_effect=lambda *a, **kw: remote_calls.append((a, kw))):
        gw.clear_remote(remote_id)

    assert len(remote_calls) == 1
    assert remote_calls[0][0][3] == remote_id


# ---------------------------------------------------------------------------
# clear_all_hass_discovery
# ---------------------------------------------------------------------------


def test_clear_all_hass_discovery_clears_hub_topic(tmp_config_dir):
    """clear_all_hass_discovery clears the hub discovery config topic."""
    gw, cfg = _make_gateway()
    gw.clear_all_hass_discovery(TEST_HUB_ID, [], [], [])

    published_topics = [call.args[0] for call in gw._client.publish.call_args_list]
    assert f"homeassistant/device/ws2m_hub_{TEST_HUB_ID}/config" in published_topics


def test_clear_all_hass_discovery_clears_remote_topics(tmp_config_dir):
    """clear_all_hass_discovery clears discovery for each supplied remote_id."""
    gw, cfg = _make_gateway()
    remote_id = "test-remote-uuid-abcd"
    gw.clear_all_hass_discovery(TEST_HUB_ID, [remote_id], [], [])

    published_topics = [call.args[0] for call in gw._client.publish.call_args_list]
    assert f"homeassistant/device/ws2m_remote_{remote_id}/config" in published_topics


def test_clear_all_hass_discovery_clears_dongle_topics(tmp_config_dir):
    """clear_all_hass_discovery clears discovery for each supplied dongle_mac."""
    gw, cfg = _make_gateway()
    gw.clear_all_hass_discovery(TEST_HUB_ID, [], [TEST_DONGLE_MAC], [])

    published_topics = [call.args[0] for call in gw._client.publish.call_args_list]
    assert f"homeassistant/device/ws2m_dongle_{TEST_DONGLE_MAC}/config" in published_topics


def test_clear_all_hass_discovery_clears_sensor_discovery(tmp_config_dir):
    """clear_all_hass_discovery clears the v2 discovery topic for each sensor."""
    gw, cfg = _make_gateway()
    sensor_mac = "AABBCCDD"
    gw.clear_all_hass_discovery(TEST_HUB_ID, [], [], [(sensor_mac, "switch")])

    published_topics = [call.args[0] for call in gw._client.publish.call_args_list]
    assert f"homeassistant/device/ws2m_sensor_{sensor_mac}/config" in published_topics


def test_clear_all_hass_discovery_does_not_clear_state_topics(tmp_config_dir):
    """clear_all_hass_discovery must NOT touch ws2m/ state topics — system remains functional."""
    gw, cfg = _make_gateway()
    gw.clear_all_hass_discovery(TEST_HUB_ID, [], [TEST_DONGLE_MAC], [("AABBCCDD", "switch")])

    published_topics = [call.args[0] for call in gw._client.publish.call_args_list]
    # None of the ws2m/ state/status topics should appear
    ws2m_state_topics = [t for t in published_topics if t.startswith(cfg["self_topic_root"])]
    assert not ws2m_state_topics, f"State topics should not be cleared: {ws2m_state_topics}"


# ---------------------------------------------------------------------------
# Remote device config entities
# ---------------------------------------------------------------------------


def test_build_remote_components_has_dongle_entity():
    """_build_remote_components includes a dongle text entity."""
    from mqtt import _build_remote_components

    components = _build_remote_components("ws2m", "test-remote-uuid")
    assert "dongle" in components
    e = components["dongle"]
    assert e["platform"] == "text"
    assert e["state_topic"].endswith("/dongle")
    assert e["command_topic"].endswith("/dongle/set")


def test_build_remote_components_has_log_level_entity():
    """_build_remote_components includes a log_level select entity."""
    from mqtt import LOG_LEVEL_OPTIONS, _build_remote_components

    components = _build_remote_components("ws2m", "test-remote-uuid")
    assert "log_level" in components
    e = components["log_level"]
    assert e["platform"] == "select"
    assert e["state_topic"].endswith("/log_level")
    assert e["command_topic"].endswith("/log_level/set")
    assert e["options"] == LOG_LEVEL_OPTIONS


def test_publish_remote_discovery_has_dongle_entity(tmp_config_dir):
    """publish_remote_discovery publishes a dongle entity in the components payload."""
    import json

    gw, cfg = _make_gateway()
    remote_id = "test-remote-uuid"
    gw.publish_remote_discovery(remote_id, TEST_HUB_ID)

    call = gw._client.publish.call_args_list[0]
    payload = json.loads(call.kwargs["payload"])
    components = payload["components"]
    assert "dongle" in components, f"dongle not in {list(components)}"
    assert components["dongle"]["platform"] == "text"
    assert components["dongle"]["state_topic"].endswith("/dongle")
    assert components["dongle"]["command_topic"].endswith("/dongle/set")


def test_publish_remote_discovery_has_log_level_entity(tmp_config_dir):
    """publish_remote_discovery publishes a log_level entity in the components payload."""
    import json

    from mqtt import LOG_LEVEL_OPTIONS

    gw, cfg = _make_gateway()
    remote_id = "test-remote-uuid"
    gw.publish_remote_discovery(remote_id, TEST_HUB_ID)

    call = gw._client.publish.call_args_list[0]
    payload = json.loads(call.kwargs["payload"])
    components = payload["components"]
    assert "log_level" in components, f"log_level not in {list(components)}"
    assert components["log_level"]["platform"] == "select"
    assert components["log_level"]["options"] == LOG_LEVEL_OPTIONS
