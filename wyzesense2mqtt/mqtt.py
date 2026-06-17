"""
MQTT gateway for WyzeSense2MQTT.

MqttGateway wraps the paho-mqtt client and owns:
  - Connection lifecycle (connect, reconnect, disconnect)
  - Publishing (JSON and raw)
  - HA MQTT discovery payload construction (device-based format, HA ≥ 2024.4)
  - Discovery schema migration (clearing stale retained topics on upgrade)

The discovery payload builders are organised as per-sensor-type component
factories so that adding a new sensor type only requires adding one entry
to _COMPONENT_BUILDERS and a corresponding entry in sensors.SENSOR_TYPES.

Discovery schema versioning
---------------------------
DISCOVERY_SCHEMA_VERSION identifies the shape of the payloads this version
publishes.  Bump it any time the topic structure or payload shape changes,
and add a cleaner to _DISCOVERY_CLEANERS for the version being replaced.

  v1 – legacy per-entity topics:
       homeassistant/<component>/wyzesense_<mac>/<entity>/config
  v2 – current device-based topic:
       homeassistant/device/wyzesense_<mac>/config
"""

import json
import logging
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt
from config import get_migration_value, set_migration_value, VERSION as WYZESENSE2MQTT_VERSION
from sensors import BINARY_SENSOR_TYPES, SENSOR_TYPES

# ---------------------------------------------------------------------------
# Discovery schema version
#
# Identifies the shape of the MQTT discovery payloads this version publishes.
# Bump this whenever the topic structure or payload shape changes, and add a
# cleaner to _DISCOVERY_CLEANERS for the version being retired.
# ---------------------------------------------------------------------------

DISCOVERY_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Per-sensor-type HA MQTT discovery component builders
#
# Each builder receives (sensor_mac, sensor_config, mac_topic) and returns
# a dict of  { entity_key: component_payload_dict }.
# Common fields (unique_id, has_entity_name, value_template) are added by
# the caller after all builders have run.
# ---------------------------------------------------------------------------


def _build_binary_sensor_components(sensor_mac: str, sensor: dict, mac_topic: str) -> dict:
    """Components for contact, motion, and similar binary sensors."""
    sensor_type = sensor.get("sensor_type", "unknown")
    type_meta = SENSOR_TYPES.get(sensor_type, SENSOR_TYPES["unknown"])
    components = {
        "state": {
            "platform": "binary_sensor",
            "name": None,
            "device_class": type_meta["device_class"],
            "payload_on": type_meta["state_on"],
            "payload_off": type_meta["state_off"],
            "json_attributes_topic": mac_topic,
        }
    }
    return components


def _build_leak_sensor_components(sensor_mac: str, sensor: dict, mac_topic: str) -> dict:
    """Components for the Wyze leak sensor (binary state + probe + climate readings)."""
    type_meta = SENSOR_TYPES["leak"]
    components = {
        "state": {
            "platform": "binary_sensor",
            "name": None,
            "device_class": type_meta["device_class"],
            "payload_on": type_meta["state_on"],
            "payload_off": type_meta["state_off"],
            "json_attributes_topic": mac_topic,
        },
        "probe_state": {
            "platform": "binary_sensor",
            "name": "Extension probe",
            "device_class": type_meta["device_class"],
            "payload_on": type_meta["state_on"],
            "payload_off": type_meta["state_off"],
            "json_attributes_topic": mac_topic,
        },
        "temperature": {
            "platform": "sensor",
            "name": None,
            "device_class": "temperature",
            "state_class": "measurement",
            "unit_of_measurement": "°C",  # leak sensors report in Celsius
            "suggested_display_precision": 1,
            "json_attributes_topic": mac_topic,
        },
        "humidity": {
            "platform": "sensor",
            "name": None,
            "device_class": "humidity",
            "state_class": "measurement",
            "unit_of_measurement": "%",
            "suggested_display_precision": 0,
            "json_attributes_topic": mac_topic,
        },
    }
    return components


def _build_climate_sensor_components(sensor_mac: str, sensor: dict, mac_topic: str) -> dict:
    """Components for the Wyze climate (temperature/humidity) sensor."""
    return {
        "temperature": {
            "platform": "sensor",
            "name": None,
            "device_class": "temperature",
            "state_class": "measurement",
            "unit_of_measurement": "°F",  # climate sensors report in Fahrenheit
            "suggested_display_precision": 1,
            "json_attributes_topic": mac_topic,
        },
        "humidity": {
            "platform": "sensor",
            "name": None,
            "device_class": "humidity",
            "state_class": "measurement",
            "unit_of_measurement": "%",
            "suggested_display_precision": 0,
            "json_attributes_topic": mac_topic,
        },
    }


# Map sensor_type → component builder.
# Binary sensors that share the same component shape use _build_binary_sensor_components.
_COMPONENT_BUILDERS: dict[str, Callable] = {
    "motion": _build_binary_sensor_components,
    "motionv2": _build_binary_sensor_components,
    "switch": _build_binary_sensor_components,
    "switchv2": _build_binary_sensor_components,
    "leak": _build_leak_sensor_components,
    "climate": _build_climate_sensor_components,
}

# Diagnostic entities appended to every sensor's component list
_DIAGNOSTIC_COMPONENTS: dict[str, dict] = {
    "signal_strength": {
        "platform": "sensor",
        "name": None,
        "device_class": "signal_strength",
        "state_class": "measurement",
        "unit_of_measurement": "dBm",
        "suggested_display_precision": 0,
        "entity_category": "diagnostic",
    },
    "battery": {
        "platform": "sensor",
        "name": None,
        "device_class": "battery",
        "state_class": "measurement",
        "unit_of_measurement": "%",
        "suggested_display_precision": 0,
        "entity_category": "diagnostic",
    },
}


# ---------------------------------------------------------------------------
# Discovery topic cleaners (one per schema version being retired)
# ---------------------------------------------------------------------------


def _clear_v1_discovery_topics(
    client: mqtt.Client,
    config: dict,
    logger: logging.Logger,
    sensor_mac: str,
    sensor_type: str,
    wait: bool = True,
) -> None:
    """Clear legacy per-entity discovery config topics (schema v1)."""
    entity_types = ["signal_strength", "battery"]
    if sensor_type in BINARY_SENSOR_TYPES:
        entity_types.append("state")
        if sensor_type == "leak":
            entity_types.append("probe_state")
            entity_types.extend(["temperature", "humidity"])
    else:
        entity_types.extend(["temperature", "humidity"])

    hass_root = config["hass_topic_root"]
    for entity_type in entity_types:
        component = "binary_sensor" if entity_type in ("state", "probe_state") else "sensor"
        _publish(
            client,
            config,
            logger,
            f"{hass_root}/{component}/wyzesense_{sensor_mac}/{entity_type}/config",
            None,
            wait=wait,
        )


def _clear_v2_discovery_topics(
    client: mqtt.Client,
    config: dict,
    logger: logging.Logger,
    sensor_mac: str,
    sensor_type: str,
    wait: bool = True,
) -> None:
    """Clear current device-based discovery config topic (schema v2)."""
    _publish(
        client,
        config,
        logger,
        f"{config['hass_topic_root']}/device/wyzesense_{sensor_mac}/config",
        None,
        wait=wait,
    )


# Map: version being migrated *away from* → its cleaner
_DISCOVERY_CLEANERS: dict[int, Callable] = {
    1: _clear_v1_discovery_topics,
    2: _clear_v2_discovery_topics,
}


# ---------------------------------------------------------------------------
# Low-level publish helper (module-level so discovery cleaners can call it
# directly without needing a MqttGateway instance)
# ---------------------------------------------------------------------------


def _publish(
    client: mqtt.Client,
    config: dict,
    logger: logging.Logger | None,
    topic: str,
    payload,
    is_json: bool = True,
    wait: bool = True,
) -> mqtt.MQTTMessageInfo:
    """Publish a single MQTT message.

    Pass payload=None to clear a retained topic (publishes an empty payload).
    """
    if payload is None:
        raw_payload = None
    elif is_json:
        raw_payload = json.dumps(payload)
    else:
        raw_payload = payload

    if logger:
        logger.debug(f"MQTT publish {topic=} {raw_payload=}")

    info = client.publish(topic, payload=raw_payload, qos=config["mqtt_qos"], retain=config["mqtt_retain"])
    if info.rc == mqtt.MQTT_ERR_SUCCESS:
        if wait:
            info.wait_for_publish(2)
    elif logger:
        logger.warning(f"MQTT publish error on {topic!r}: {mqtt.error_string(info.rc)}")
    return info


# ---------------------------------------------------------------------------
# MqttGateway
# ---------------------------------------------------------------------------


class MqttGateway:
    """Owns the MQTT client connection and all publish/subscribe operations."""

    def __init__(self, config: dict, logger: logging.Logger | None = None):
        self._config = config
        self._logger = logger.getChild("mqtt") if logger else logging.getLogger("ws2m.mqtt")
        self._client: mqtt.Client | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(
        self,
        on_connect: Callable,
        on_disconnect: Callable,
        on_message: Callable,
    ) -> None:
        """Create the paho client, configure callbacks, and begin connecting.

        Blocks until the connection is established (or raises on failure).
        The supplied callbacks follow the paho v2 signature:
            on_connect(client, userdata, flags, reason_code, properties)
            on_disconnect(client, userdata, flags, reason_code, properties)
            on_message(client, userdata, msg)
        """
        cfg = self._config
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg["mqtt_client_id"],
            clean_session=cfg["mqtt_clean_session"],
        )
        self._client.username_pw_set(username=cfg["mqtt_username"], password=cfg["mqtt_password"])
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)
        self._client.on_connect = on_connect
        self._client.on_disconnect = on_disconnect
        self._client.on_message = on_message
        self._client.enable_logger(self._logger)

        # connected_flag is a lightweight indicator the bridge main loop can poll
        self._client.connected_flag = False

        self._logger.info(f"Connecting to MQTT broker {cfg['mqtt_host']}:{cfg['mqtt_port']}")
        self._client.connect_async(cfg["mqtt_host"], port=cfg["mqtt_port"], keepalive=cfg["mqtt_keepalive"])
        self._client.loop_start()

        # Wait for on_connect to fire
        while not self._client.connected_flag:
            time.sleep(0.1)

    def disconnect(self) -> None:
        """Stop the network loop and disconnect cleanly."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    @property
    def client(self) -> mqtt.Client:
        """Raw paho client, for callers that need subscribe/callback_add."""
        if self._client is None:
            raise RuntimeError("MqttGateway.connect() has not been called")
        return self._client

    @property
    def is_connected(self) -> bool:
        """Return True if the client has an active broker connection."""
        return bool(self._client and self._client.connected_flag)

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(self, topic: str, payload, is_json: bool = True, wait: bool = True) -> mqtt.MQTTMessageInfo:
        """Publish to *topic*.  Pass payload=None to clear a retained topic."""
        return _publish(self._client, self._config, self._logger, topic, payload, is_json=is_json, wait=wait)

    # ------------------------------------------------------------------
    # Bridge-level discovery
    # ------------------------------------------------------------------

    def publish_bridge_discovery(self, dongle_mac: str, dongle_version: str, wait: bool = True) -> None:
        """Publish the HA discovery config for the bridge connection-state entity."""
        cfg = self._config
        state_topic = f"{cfg['self_topic_root']}/bridge_{dongle_mac}/status"
        payload = {
            "default_entity_id": "binary_sensor.ws2m_bridge_connection_state",
            "device": {
                "hw_version": dongle_version,
                "identifiers": [f"ws2m_bridge_{dongle_mac}", dongle_mac],
                "manufacturer": "Raetha",
                "model": "Bridge",
                "name": f"WyzeSense2MQTT Bridge {dongle_mac}",
                "sw_version": WYZESENSE2MQTT_VERSION,
            },
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "has_entity_name": True,
            "name": "Connection state",
            "object_id": "ws2m_bridge_connection_state",
            "origin": {
                "name": "WyzeSense2MQTT",
                "sw_version": WYZESENSE2MQTT_VERSION,
                "support_url": "https://github.com/raetha/wyzesense2mqtt",
            },
            "payload_off": "offline",
            "payload_on": "online",
            "qos": cfg["mqtt_qos"],
            "state_topic": state_topic,
            "unique_id": f"ws2m_bridge_{dongle_mac}_connection_state",
        }
        topic = f"{cfg['hass_topic_root']}/binary_sensor/ws2m_bridge_{dongle_mac}/connection_state/config"
        self.publish(topic, payload, wait=wait)

    # ------------------------------------------------------------------
    # Sensor discovery
    # ------------------------------------------------------------------

    def publish_sensor_discovery(
        self,
        sensor_mac: str,
        sensor: dict,
        dongle_mac: str,
        sensor_online: bool,
        wait: bool = True,
    ) -> None:
        """Build and publish the device-based HA discovery payload for one sensor.

        Uses the v2 device-based format:
          homeassistant/device/wyzesense_<mac>/config
        """
        cfg = self._config
        sensor_type = sensor.get("sensor_type", "unknown")

        builder = _COMPONENT_BUILDERS.get(sensor_type)
        if builder is None:
            self._logger.error(f"No discovery component builder for sensor type {sensor_type!r} ({sensor_mac})")
            return

        mac_topic = f"{cfg['self_topic_root']}/{sensor_mac}"
        components: dict = builder(sensor_mac, sensor, mac_topic)
        components.update(_DIAGNOSTIC_COMPONENTS)

        # Inject common per-component fields
        for entity_key, component in components.items():
            component["unique_id"] = f"wyzesense_{sensor_mac}_{entity_key}"
            component["has_entity_name"] = True
            component["value_template"] = f"{{{{ value_json.{entity_key} }}}}"

        type_meta = SENSOR_TYPES.get(sensor_type, SENSOR_TYPES["unknown"])
        device_payload = {
            "device": {
                "identifiers": [f"wyzesense_{sensor_mac}", sensor_mac],
                "manufacturer": "WyzeLabs",
                "model": sensor.get("model", type_meta.get("model", "WyzeSense Sensor")),
                "hw_version": sensor.get("hw_version", type_meta.get("hw_version", "unknown")),
                "name": sensor.get("name", f"WyzeSense {sensor_mac}"),
                "sw_version": sensor.get("sw_version", "unknown"),
                "via_device": f"ws2m_bridge_{dongle_mac}",
            },
            "origin": {
                "name": "WyzeSense2MQTT",
                "sw_version": WYZESENSE2MQTT_VERSION,
                "support_url": "https://github.com/raetha/wyzesense2mqtt",
            },
            "schema_version": DISCOVERY_SCHEMA_VERSION,
            "components": components,
            "state_topic": mac_topic,
            "availability": [
                {"topic": f"{cfg['self_topic_root']}/{sensor_mac}/status"},
                {"topic": f"{cfg['self_topic_root']}/bridge_{dongle_mac}/status"},
            ],
            "availability_mode": "all",
            "qos": cfg["mqtt_qos"],
        }

        device_topic = f"{cfg['hass_topic_root']}/device/wyzesense_{sensor_mac}/config"
        self.publish(device_topic, device_payload, wait=wait)
        self._logger.debug(f"Published discovery for {sensor_mac} → {device_topic}")

        # Publish initial availability
        self.publish(
            f"{cfg['self_topic_root']}/{sensor_mac}/status",
            "online" if sensor_online else "offline",
            is_json=False,
            wait=wait,
        )

    # ------------------------------------------------------------------
    # Sensor topic cleanup
    # ------------------------------------------------------------------

    def clear_sensor_topics(self, sensor_mac: str, sensor_type: str, wait: bool = True) -> None:
        """Clear all retained topics for a sensor (used on removal)."""
        cfg = self._config
        self._logger.info(f"Clearing MQTT topics for {sensor_mac}")
        self.publish(f"{cfg['self_topic_root']}/{sensor_mac}/status", None, wait=wait)
        self.publish(f"{cfg['self_topic_root']}/{sensor_mac}", None, wait=wait)

        if cfg["hass_discovery"]:
            self.clear_all_discovery_topics(sensor_mac, sensor_type, wait=wait)

    def clear_all_discovery_topics(self, sensor_mac: str, sensor_type: str, wait: bool = True) -> None:
        """Clear discovery topics from every known schema version for *sensor_mac*.

        Safe to call regardless of which version originally published the config;
        used for full sensor removal so no stale entities remain.
        """
        for cleaner in _DISCOVERY_CLEANERS.values():
            cleaner(self._client, self._config, self._logger, sensor_mac, sensor_type, wait=wait)

    # ------------------------------------------------------------------
    # Discovery schema migration
    # ------------------------------------------------------------------

    def get_discovery_schema_version(self) -> int:
        """Return the last-recorded discovery schema version (default 1 for pre-tracking installs)."""
        return get_migration_value("discovery_schema_version", self._logger) or 1

    def set_discovery_schema_version(self, version: int) -> None:
        """Persist the current discovery schema version after migrations have run."""
        set_migration_value("discovery_schema_version", version, self._logger)

    def migrate_discovery_topics(
        self,
        sensor_mac: str,
        sensor_type: str,
        from_version: int,
        wait: bool = True,
    ) -> None:
        """Clear retained topics for every schema version between *from_version*
        (inclusive) and DISCOVERY_SCHEMA_VERSION (exclusive).
        """
        for version in range(from_version, DISCOVERY_SCHEMA_VERSION):
            cleaner = _DISCOVERY_CLEANERS.get(version)
            if cleaner:
                self._logger.debug(f"Clearing v{version} discovery topics for {sensor_mac}")
                cleaner(self._client, self._config, self._logger, sensor_mac, sensor_type, wait=wait)
