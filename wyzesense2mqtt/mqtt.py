"""
MQTT gateway for WyzeSense2MQTT.

MqttGateway wraps the paho-mqtt client and owns:
  - Connection lifecycle (connect, reconnect, disconnect)
  - Publishing (JSON and raw)
  - HA MQTT discovery payload construction (device-based format, HA ≥ 2024.4)
  - Discovery schema migration (clearing stale retained topics on upgrade)

Sensor component architecture
------------------------------
Each sensor type maps to a *component builder* function that returns a fresh
dict of ``{ entity_key: component_payload_dict }``.  Two additional builders
are appended to every sensor regardless of type:

  _build_diagnostic_components()               — signal_strength, battery
  _build_sensor_action_components(mac, topic)  — remove button

Builders always return *new* dicts so the inject loop can safely mutate the
component payloads (stamping in unique_id, has_entity_name, value_template)
without corrupting any shared module-level state.

Adding a new universal sensor entity is a one-line addition to
_build_diagnostic_components() or _build_sensor_action_components().
Adding a new bridge entity means editing _build_bridge_components().
Adding a new sensor type means adding a builder and registering it in
_COMPONENT_BUILDERS.

Discovery schema versioning
----------------------------
DISCOVERY_SCHEMA_VERSION is a single project-wide constant that covers both
sensor and bridge discovery payloads.  Bump it whenever *any* topic structure
or payload shape changes, and add the corresponding cleanup to
``migrate_to_v<N>`` in the _MIGRATION_STEPS list below.

Schema history
  v1 — legacy per-entity sensor topics + 3.x bridge topic:
         homeassistant/<platform>/wyzesense_<mac>/<entity>/config
         homeassistant/binary_sensor/wyzesense_bridge_<mac>/connection_state/config
  v2 — current device-based topics for both sensors and bridge:
         homeassistant/device/wyzesense_<mac>/config
         homeassistant/device/ws2m_bridge_<mac>/config

The single ``discovery_schema_version`` key in migrations.yaml tracks where
each installation is up to.  Bridge and sensor topics are migrated together in
one pass, removing the need for a separate ``bridge_identity_version`` key.
"""

import json
import logging
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt
from config import VERSION, get_migration_value, set_migration_value
from sensors import BINARY_SENSOR_TYPES, SENSOR_TYPES

# ---------------------------------------------------------------------------
# Discovery schema version
#
# Single project-wide constant covering sensor and bridge payloads together.
# Bump this whenever any topic structure or payload shape changes, and add a
# corresponding migrate_to_vN entry to _MIGRATION_STEPS below.
# ---------------------------------------------------------------------------

DISCOVERY_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Per-sensor-type HA MQTT discovery component builders
#
# Each builder receives (sensor_mac, sensor_config, mac_topic) and returns
# a *fresh* dict of { entity_key: component_payload_dict }.
# Common fields (unique_id, has_entity_name, value_template) are injected by
# publish_sensor_discovery after all builders have run.
# ---------------------------------------------------------------------------


def _build_state_sensor_components(sensor_mac: str, sensor: dict, mac_topic: str) -> dict:
    """Components for sensors that report a single on/off state (contact, motion, PIR).

    Motion and contact sensors share the same HA component shape — one
    binary_sensor entity whose device_class and payload labels come from
    SENSOR_TYPES.  Use this builder for any sensor_type that maps to a
    single boolean reading with no additional entities.
    """
    sensor_type = sensor.get("sensor_type", "unknown")
    type_meta = SENSOR_TYPES.get(sensor_type, SENSOR_TYPES["unknown"])
    return {
        "state": {
            "platform": "binary_sensor",
            "name": None,
            "device_class": type_meta["device_class"],
            "payload_on": type_meta["state_on"],
            "payload_off": type_meta["state_off"],
            "json_attributes_topic": mac_topic,
        }
    }


def _build_leak_sensor_components(sensor_mac: str, sensor: dict, mac_topic: str) -> dict:
    """Components for the Wyze leak sensor (binary state + probe + climate readings)."""
    type_meta = SENSOR_TYPES["leak"]
    return {
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
# Sensors that report a single on/off state share _build_state_sensor_components.
_COMPONENT_BUILDERS: dict[str, Callable] = {
    "motion": _build_state_sensor_components,
    "motionv2": _build_state_sensor_components,
    "switch": _build_state_sensor_components,
    "switchv2": _build_state_sensor_components,
    "leak": _build_leak_sensor_components,
    "climate": _build_climate_sensor_components,
}


def _build_diagnostic_components() -> dict:
    """Diagnostic entities appended to every sensor's component list.

    Returns a fresh dict on every call so the inject loop can safely mutate
    the component payloads without corrupting shared module-level state.
    To add a new universal diagnostic entity, add an entry here.
    """
    return {
        "signal_strength": {
            "platform": "sensor",
            "name": None,
            "device_class": "signal_strength",
            "state_class": "measurement",
            "unit_of_measurement": "dBm",
            "suggested_display_precision": 0,
            "entity_category": "diagnostic",
            "enabled_by_default": False,
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


def _build_sensor_action_components(sensor_mac: str, remove_topic: str) -> dict:
    """Per-sensor action buttons appended to every sensor's component list.

    Returns a fresh dict on every call.  The remove button publishes the
    sensor's MAC as the payload, which the bridge handles via its
    ``ws2m/remove`` subscription.
    To add a new universal sensor action, add an entry here.
    """
    return {
        "remove": {
            "platform": "button",
            "name": "Remove sensor",
            "entity_category": "config",
            "device_class": "restart",
            "command_topic": remove_topic,
            "payload_press": sensor_mac,
        },
    }


# Platform types that expose a state value — value_template is injected only for these
_STATE_PLATFORMS: frozenset[str] = frozenset({"sensor", "binary_sensor"})


# ---------------------------------------------------------------------------
# Bridge component builder
# ---------------------------------------------------------------------------


def _build_bridge_components(self_root: str, dongle_mac: str) -> dict:
    """Components for the bridge device.

    Returns a fresh dict on every call.  To add a new bridge entity,
    add an entry here — no other code needs to change.
    """
    return {
        "connection_state": {
            "platform": "binary_sensor",
            "name": "Connection state",
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "payload_on": "online",
            "payload_off": "offline",
            "state_topic": f"{self_root}/bridge_{dongle_mac}/status",
            "unique_id": f"ws2m_bridge_{dongle_mac}_connection_state",
        },
        "scan": {
            "platform": "button",
            "name": "Scan for sensor",
            "entity_category": "config",
            "command_topic": f"{self_root}/scan",
            "payload_press": "scan",
            "unique_id": f"ws2m_bridge_{dongle_mac}_scan",
        },
        "reload": {
            "platform": "button",
            "name": "Reload config",
            "entity_category": "config",
            "command_topic": f"{self_root}/reload",
            "payload_press": "reload",
            "unique_id": f"ws2m_bridge_{dongle_mac}_reload",
        },
    }


# ---------------------------------------------------------------------------
# Discovery schema migration
#
# Each entry in _MIGRATION_STEPS is a callable:
#   migrate_to_vN(client, config, logger, sensor_mac, sensor_type, wait)
#
# The function clears *all* stale retained topics that were published by
# version N-1 — for both sensors and the bridge.  Sensor-specific cleanup
# receives the sensor MAC and type; bridge-specific cleanup uses only config.
#
# Adding a migration for a future version N:
#   1. Write def _migrate_to_vN(...)
#   2. Append it to _MIGRATION_STEPS
#   3. Bump DISCOVERY_SCHEMA_VERSION to N
#
# _MIGRATION_STEPS is indexed from 0; step at index i clears v(i+1) topics,
# i.e. step 0 clears v1 topics (the migration from v1 → v2).
# ---------------------------------------------------------------------------


def _migrate_to_v2(
    client: mqtt.Client,
    config: dict,
    logger: logging.Logger,
    sensor_mac: str,
    sensor_type: str,
    dongle_mac: str,
    wait: bool = True,
) -> None:
    """Clear all v1 retained topics for one sensor and (once) the bridge.

    v1 sensor topics — legacy per-entity format:
      homeassistant/<platform>/wyzesense_<mac>/<entity>/config

    v1 bridge topic — 3.x identity:
      homeassistant/binary_sensor/wyzesense_bridge_<mac>/connection_state/config

    The bridge topic is keyed on dongle_mac, not sensor_mac, so it is cleared
    here on every sensor's migration pass (publishing None to a topic that is
    already empty is a safe no-op).
    """
    hass_root = config["hass_topic_root"]

    # --- v1 sensor per-entity topics ---
    entity_types = ["signal_strength", "battery"]
    if sensor_type in BINARY_SENSOR_TYPES:
        entity_types.append("state")
        if sensor_type == "leak":
            entity_types.append("probe_state")
            entity_types.extend(["temperature", "humidity"])
    else:
        entity_types.extend(["temperature", "humidity"])

    for entity_type in entity_types:
        platform = "binary_sensor" if entity_type in ("state", "probe_state") else "sensor"
        topic = f"{hass_root}/{platform}/wyzesense_{sensor_mac}/{entity_type}/config"
        _publish(client, config, logger, topic, None, wait=wait)

    # --- v1 bridge topic (3.x identity) ---
    bridge_topic = f"{hass_root}/binary_sensor/wyzesense_bridge_{dongle_mac}/connection_state/config"
    _publish(client, config, logger, bridge_topic, None, wait=wait)


# List of migration step functions, one per schema version transition.
# Index 0 = migration to v2 (clears v1 topics), index 1 = migration to v3, etc.
_MIGRATION_STEPS: list[Callable] = [
    _migrate_to_v2,
]


# ---------------------------------------------------------------------------
# Low-level publish helper (module-level so migration steps can call it
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
# Module-level sensor discovery cleanup helper
#
# Used by cli/maintenance.py which operates without a MqttGateway instance.
# ---------------------------------------------------------------------------


def clear_sensor_discovery_topics(
    client: mqtt.Client,
    config: dict,
    logger: logging.Logger | None,
    sensor_mac: str,
    sensor_type: str,
    wait: bool = True,
) -> None:
    """Clear the sensor's discovery topic from every known schema version.

    Covers all versions so it is safe to call regardless of which schema
    version originally published the sensor's config topics.
    """
    hass_root = config["hass_topic_root"]

    # v1 per-entity sensor topics
    entity_types = ["signal_strength", "battery"]
    if sensor_type in BINARY_SENSOR_TYPES:
        entity_types.append("state")
        if sensor_type == "leak":
            entity_types.append("probe_state")
            entity_types.extend(["temperature", "humidity"])
    else:
        entity_types.extend(["temperature", "humidity"])
    for entity_type in entity_types:
        platform = "binary_sensor" if entity_type in ("state", "probe_state") else "sensor"
        topic = f"{hass_root}/{platform}/wyzesense_{sensor_mac}/{entity_type}/config"
        _publish(client, config, logger, topic, None, wait=wait)

    # v2 device-based sensor topic
    _publish(client, config, logger, f"{hass_root}/device/wyzesense_{sensor_mac}/config", None, wait=wait)


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
    # Bridge discovery
    # ------------------------------------------------------------------

    def publish_bridge_discovery(self, dongle_mac: str, dongle_version: str, wait: bool = True) -> None:
        """Publish the HA device-based discovery config for the bridge.

        Publishes a single retained topic:
          homeassistant/device/ws2m_bridge_<mac>/config

        The payload's ``components`` block is built by _build_bridge_components().
        To add a new bridge entity, edit that function — no other code changes.
        """
        cfg = self._config
        components = _build_bridge_components(cfg["self_topic_root"], dongle_mac)
        for component in components.values():
            component["has_entity_name"] = True

        payload = {
            "device": {
                "hw_version": dongle_version,
                "identifiers": [f"ws2m_bridge_{dongle_mac}", dongle_mac],
                "manufacturer": "Raetha",
                "model": "Bridge",
                "name": f"WyzeSense2MQTT Bridge {dongle_mac}",
                "sw_version": VERSION,
            },
            "origin": {
                "name": "WyzeSense2MQTT",
                "sw_version": VERSION,
                "support_url": "https://github.com/raetha/wyzesense2mqtt",
            },
            "components": components,
            "qos": cfg["mqtt_qos"],
        }
        topic = f"{cfg['hass_topic_root']}/device/ws2m_bridge_{dongle_mac}/config"
        self.publish(topic, payload, wait=wait)
        self._logger.debug(f"Published bridge discovery → {topic}")

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

        The payload's ``components`` block is assembled from three sources:
          1. The sensor-type-specific builder from _COMPONENT_BUILDERS
          2. _build_diagnostic_components() — universal diagnostic entities
          3. _build_sensor_action_components() — universal action buttons

        All builders return fresh dicts, so the inject loop below can safely
        stamp common fields (unique_id, has_entity_name, value_template) into
        the component payloads without risk of mutating shared module state.
        """
        cfg = self._config
        sensor_type = sensor.get("sensor_type", "unknown")

        builder = _COMPONENT_BUILDERS.get(sensor_type)
        if builder is None:
            self._logger.error(f"No discovery component builder for sensor type {sensor_type!r} ({sensor_mac})")
            return

        mac_topic = f"{cfg['self_topic_root']}/{sensor_mac}"
        remove_topic = f"{cfg['self_topic_root']}/remove"

        # Merge all component sources — all return fresh dicts, safe to mutate below
        components: dict = builder(sensor_mac, sensor, mac_topic)
        components.update(_build_diagnostic_components())
        components.update(_build_sensor_action_components(sensor_mac, remove_topic))

        # Inject common per-component fields.
        # value_template is only meaningful for state-bearing platforms;
        # skip it for action-only platforms such as button.
        for entity_key, component in components.items():
            component["unique_id"] = f"wyzesense_{sensor_mac}_{entity_key}"
            component["has_entity_name"] = True
            if component.get("platform") in _STATE_PLATFORMS:
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
                "sw_version": VERSION,
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
            self.clear_all_sensor_discovery_topics(sensor_mac, sensor_type, wait=wait)

    def clear_all_sensor_discovery_topics(self, sensor_mac: str, sensor_type: str, wait: bool = True) -> None:
        """Clear the sensor's discovery topic from every known schema version.

        Safe to call regardless of which version originally published the config;
        used for full sensor removal so no stale entities remain in HA.
        """
        clear_sensor_discovery_topics(self._client, self._config, self._logger, sensor_mac, sensor_type, wait=wait)

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
        dongle_mac: str,
        from_version: int,
        wait: bool = True,
    ) -> None:
        """Clear stale retained topics for all schema versions from *from_version* to current.

        Each migration step handles both sensor and bridge topics for that version
        transition, so bridge and sensor discovery are always migrated together in
        a single pass driven by bridge.py's _init_sensors loop.
        """
        for step_index in range(from_version - 1, DISCOVERY_SCHEMA_VERSION - 1):
            if step_index < len(_MIGRATION_STEPS):
                target_version = step_index + 2
                self._logger.debug(f"Clearing v{target_version - 1} discovery topics for {sensor_mac}")
                _MIGRATION_STEPS[step_index](
                    self._client, self._config, self._logger, sensor_mac, sensor_type, dongle_mac, wait=wait
                )
