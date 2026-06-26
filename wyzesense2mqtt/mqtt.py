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
dict of ``{ entity_key: component_payload_dict }``.  Additional builders
are appended to every sensor regardless of type:

  _build_diagnostic_components()               — signal_strength, battery
  _build_sensor_action_components(mac, topic)  — remove button
  _build_sensor_config_components(mac, sensor, mac_topic) — name/class/invert

Builders always return *new* dicts so the inject loop can safely mutate the
component payloads (stamping in unique_id, has_entity_name, value_template)
without corrupting any shared module-level state.

Adding a new universal sensor entity is a one-line addition to
_build_diagnostic_components() or _build_sensor_action_components().
Adding a new dongle entity means editing _build_dongle_components().
Adding a new service entity means editing _build_service_components().
Adding a new sensor type means adding a builder and registering it in
_COMPONENT_BUILDERS.

HA device hierarchy
--------------------
  ws2m_service_<uuid>     — software service device (singleton per ws2m instance)
    └─ ws2m_dongle_<mac>  — physical USB dongle device (one per connected dongle)
         └─ wyzesense_<mac> — individual Wyze sensor

Discovery schema versioning
----------------------------
DISCOVERY_SCHEMA_VERSION is a single project-wide constant that covers all
discovery payloads.  Bump it whenever *any* topic structure or payload shape
changes, and add the corresponding cleanup to ``migrate_to_v<N>`` in the
_MIGRATION_STEPS list below.

Schema history
  v1 — legacy per-entity sensor topics + 3.x bridge topic:
         homeassistant/<platform>/wyzesense_<mac>/<entity>/config
         homeassistant/binary_sensor/wyzesense_bridge_<mac>/connection_state/config
  v2 — device-based topics; service + dongle split (4.0.0):
         homeassistant/device/wyzesense_<mac>/config       (sensor)
         homeassistant/device/ws2m_service_<uuid>/config   (software service)
         homeassistant/device/ws2m_dongle_<mac>/config     (hardware dongle)

The single ``discovery_schema_version`` key in migrations.yaml tracks where
each installation is up to.  All device and sensor topics are migrated
together in one pass.

MQTT QoS and retain policy
----------------------------
QoS and retain values are hardcoded per-message type rather than being user-
configurable.  This matches the behaviour of modern bridges (e.g. Zigbee2MQTT)
and ensures sensible defaults without burdening users with MQTT semantics.

  Status topics (online/offline):  QoS 1, retain=True
    — Retained so HA recovers the correct availability state after a restart.

  Discovery topics:                QoS 1, retain=True
    — Retained so HA re-imports entity config after a restart without waiting
      for the next bridge publish cycle.

  Data topics (sensor payloads):   QoS 0, retain=False
    — High-frequency; at-most-once delivery is appropriate.  Not retained so
      HA does not display stale sensor readings after a long silence.

  Command topics (button/select):  QoS 1, retain=False
    — At-least-once delivery so commands are not silently dropped, but not
      retained so commands do not replay on reconnect.

  Number state topics (chime):     QoS 1, retain=True
    — Retained so number entities recover their current value in HA after restart.
"""

import json
import logging
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt
from config import VERSION, get_migration_value, set_migration_value
from sensors import BINARY_SENSOR_TYPES, DEVICE_CLASS_OPTIONS, INVERTIBLE_SENSOR_TYPES, SENSOR_TYPES

# ---------------------------------------------------------------------------
# Fixed constants
# ---------------------------------------------------------------------------

# Discovery schema version — bump when any topic/payload shape changes.
DISCOVERY_SCHEMA_VERSION = 2

# Valid log level choices exposed via the HA log_level select entity.
LOG_LEVEL_OPTIONS: list[str] = ["DEBUG", "INFO", "WARNING", "ERROR"]

# ---------------------------------------------------------------------------
# Per-message QoS / retain constants
# ---------------------------------------------------------------------------

_QOS_STATUS = 1
_QOS_DISCOVERY = 1
_QOS_DATA = 0
_QOS_COMMAND = 1
_QOS_NUMBER = 1

_RETAIN_STATUS = True
_RETAIN_DISCOVERY = True
_RETAIN_DATA = False
_RETAIN_COMMAND = False
_RETAIN_NUMBER = True


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

    invert_state: when True the payload_on/payload_off values are swapped so
    HA interprets the hardware state in reverse — useful for sensors installed
    in a non-standard orientation (e.g. a contact sensor inside a doorbell
    chime box that reads 'closed' when the bell rings).  The raw MQTT payload
    from the sensor is unchanged; only the HA discovery config differs.
    """
    sensor_type = sensor.get("sensor_type", "unknown")
    type_meta = SENSOR_TYPES.get(sensor_type, SENSOR_TYPES["unknown"])

    # Use per-sensor device_class override if set; fall back to type default.
    device_class = sensor.get("class", type_meta.get("device_class", "opening"))

    payload_on = type_meta.get("state_on", "open")
    payload_off = type_meta.get("state_off", "closed")

    # Swap payloads for invert_state.  Only applicable to invertible types.
    if sensor.get("invert_state") and sensor_type in INVERTIBLE_SENSOR_TYPES:
        payload_on, payload_off = payload_off, payload_on

    return {
        "state": {
            "platform": "binary_sensor",
            "name": None,
            "device_class": device_class,
            "payload_on": payload_on,
            "payload_off": payload_off,
            "json_attributes_topic": mac_topic,
        }
    }


def _build_leak_sensor_components(sensor_mac: str, sensor: dict, mac_topic: str, probe_available: bool = True) -> dict:
    """Components for the Wyze leak sensor (binary state + optional probe entities).

    probe_available controls whether probe_state is included.  When no probe is
    connected the probe entities are omitted rather than left permanently unavailable.
    """
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
    if probe_available:
        components["probe_state"] = {
            "platform": "binary_sensor",
            "name": "Extension probe",
            "device_class": type_meta["device_class"],
            "payload_on": type_meta["state_on"],
            "payload_off": type_meta["state_off"],
            "json_attributes_topic": mac_topic,
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


def _build_keypad_components(sensor_mac: str, sensor: dict, mac_topic: str) -> dict:
    """Components for the Wyze Sense V2 Keypad.

    The keypad publishes three distinct event types to the same MAC topic:
      keypad_mode       — alarm_control_panel state (disarmed/armed_home/armed_away/triggered)
      keypad_motion     — PIR motion sensor on the keypad face
      keypad_pin_*      — PIN entry events (not exposed as HA entities; for automation use only)

    PIN management entities:
      pin_count  — sensor showing number of configured PINs (read-only)
      add_pin    — button that arms ws2m to capture the next hardware PIN entry
      clear_pins — button that wipes all configured PINs
    """
    command_topic = f"{mac_topic}/set"
    pin_count = sensor.get("pins", [])
    if isinstance(pin_count, str):
        pin_count = [pin_count] if pin_count else []
    return {
        "alarm_mode": {
            "platform": "alarm_control_panel",
            "name": None,
            "state_topic": mac_topic,
            "command_topic": command_topic,
            "value_template": "{{ value_json.alarm_mode }}",
            "payload_disarm": "disarmed",
            "payload_arm_home": "armed_home",
            "payload_arm_away": "armed_away",
            "payload_trigger": "triggered",
            "json_attributes_topic": mac_topic,
        },
        "motion": {
            "platform": "binary_sensor",
            "name": "Motion",
            "device_class": "motion",
            "payload_on": "active",
            "payload_off": "inactive",
            "json_attributes_topic": mac_topic,
        },
        "pin_count": {
            "platform": "sensor",
            "name": "PIN count",
            "state_topic": f"{mac_topic}/pin_count",
            "entity_category": "config",
        },
        "add_pin": {
            "platform": "button",
            "name": "Arm PIN capture",
            "entity_category": "config",
            "command_topic": f"{mac_topic}/add_pin",
            "payload_press": "arm",
        },
        "clear_pins": {
            "platform": "button",
            "name": "Clear all PINs",
            "entity_category": "config",
            "command_topic": f"{mac_topic}/clear_pins",
            "payload_press": "clear",
        },
    }


def _build_chime_components(sensor_mac: str, sensor: dict, mac_topic: str) -> dict:
    """Components for the Wyze Sense Chime (plug-in RF speaker, Wyze Doorbell V1 accessory).

    The chime is output-only.  ws2m sends CMD_PLAY_CHIME (0x70) when commanded.
    Three configurable parameters are exposed as HA number entities so the user
    can adjust them from the device page without editing sensors.yaml directly.
    ws2m persists changes back to sensors.yaml when values are updated via MQTT.

    ring_id valid values and tone mapping are undocumented; the full range 0–255
    is allowed.  See docs/protocol.md for how to explore ring IDs.
    """
    return {
        "play": {
            "platform": "button",
            "name": None,
            "command_topic": f"{mac_topic}/play",
            "payload_press": "PLAY",
            "device_class": "sound",
        },
        "ring_id": {
            "platform": "number",
            "name": "Ring tone",
            "state_topic": f"{mac_topic}/ring_id",
            "command_topic": f"{mac_topic}/ring_id/set",
            "min": 0,
            "max": 255,
            "step": 1,
            "mode": "box",
        },
        "volume": {
            "platform": "number",
            "name": "Volume",
            "state_topic": f"{mac_topic}/volume",
            "command_topic": f"{mac_topic}/volume/set",
            "min": 1,
            "max": 9,
            "step": 1,
            "mode": "slider",
        },
        "repeat_count": {
            "platform": "number",
            "name": "Repeat count",
            "state_topic": f"{mac_topic}/repeat_count",
            "command_topic": f"{mac_topic}/repeat_count/set",
            "min": 1,
            "max": 9,
            "step": 1,
            "mode": "box",
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
    "chime": _build_chime_components,
    "keypad": _build_keypad_components,
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
        "battery_voltage": {
            "platform": "sensor",
            "name": "Battery voltage",
            "device_class": "voltage",
            "state_class": "measurement",
            "unit_of_measurement": "V",
            "suggested_display_precision": 3,
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
        "die_temp": {
            "platform": "sensor",
            "name": "Chip temperature",
            "device_class": "temperature",
            "state_class": "measurement",
            "unit_of_measurement": "°C",
            "suggested_display_precision": 0,
            "entity_category": "diagnostic",
            "enabled_by_default": False,
        },
    }


def _build_sensor_action_components(sensor_mac: str, remove_topic: str) -> dict:
    """Per-sensor action buttons appended to every sensor's component list.

    Returns a fresh dict on every call.  The remove button publishes the
    sensor's MAC as the payload, which the dongle worker handles via its
    ``ws2m/dongle_<mac>/remove`` subscription.
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


def _build_sensor_config_components(sensor_mac: str, sensor: dict, mac_topic: str) -> dict:
    """Configuration entities appended to every sensor's component list.

    Exposes editable sensor settings as HA entities so users can adjust them
    from the HA device page without editing sensors.yaml.  Changes are published
    back to ws2m via MQTT command topics and persisted to sensors.yaml.

    Entities:
      sensor_name   — text entity for the HA device display name
      device_class  — select entity to choose HA binary_sensor device_class
                      (only for sensor types that have selectable options)
      invert_state  — switch entity to reverse payload_on/payload_off in HA
                      (only for invertible sensor types)
    """
    sensor_type = sensor.get("sensor_type", "unknown")
    components: dict = {
        "sensor_name": {
            "platform": "text",
            "name": "Sensor name",
            "entity_category": "config",
            "state_topic": f"{mac_topic}/sensor_name",
            "command_topic": f"{mac_topic}/sensor_name/set",
            "max": 64,
        },
    }

    # device_class select — only for sensor types with multiple class options
    if sensor_type in DEVICE_CLASS_OPTIONS:
        options = DEVICE_CLASS_OPTIONS[sensor_type]
        components["device_class"] = {
            "platform": "select",
            "name": "Device class",
            "entity_category": "config",
            "state_topic": f"{mac_topic}/device_class",
            "command_topic": f"{mac_topic}/device_class/set",
            "options": options,
        }

    # invert_state switch — only for invertible sensor types
    if sensor_type in INVERTIBLE_SENSOR_TYPES:
        components["invert_state"] = {
            "platform": "switch",
            "name": "Invert state",
            "entity_category": "config",
            "state_topic": f"{mac_topic}/invert_state",
            "command_topic": f"{mac_topic}/invert_state/set",
            "payload_on": "true",
            "payload_off": "false",
        }

    return components


# Platform types that expose a state value via the shared mac_topic JSON payload —
# value_template is injected only for these.
# Excluded platforms manage their own state topics or have no state topic:
#   alarm_control_panel — value_template set explicitly in _build_keypad_components
#   button              — command-only, no state topic
#   number              — each entity has its own dedicated state_topic
#   select              — own state_topic (sensor config entities)
#   switch              — own state_topic (invert_state)
#   text                — own state_topic (sensor_name)
_STATE_PLATFORMS: frozenset[str] = frozenset({"sensor", "binary_sensor"})


# ---------------------------------------------------------------------------
# Service component builder
# ---------------------------------------------------------------------------


def _build_service_components(self_root: str) -> dict:
    """Components for the ws2m software service device (singleton per instance).

    The service device owns the reload button, log level select, and any other
    service-level controls that span all connected dongles.

    Returns a fresh dict on every call.  To add a new service-level entity,
    add an entry here — no other code needs to change.
    """
    return {
        "reload": {
            "platform": "button",
            "name": "Reload config",
            "entity_category": "config",
            "command_topic": f"{self_root}/reload",
            "payload_press": "reload",
        },
        "log_level": {
            "platform": "select",
            "name": "Log level",
            "entity_category": "config",
            "state_topic": f"{self_root}/log_level",
            "command_topic": f"{self_root}/log_level/set",
            "options": LOG_LEVEL_OPTIONS,
        },
        "cleanup_removed_dongles": {
            "platform": "button",
            "name": "Cleanup removed dongles",
            "entity_category": "config",
            "command_topic": f"{self_root}/cleanup_removed_dongles",
            "payload_press": "cleanup",
        },
    }


# ---------------------------------------------------------------------------
# Dongle component builder
# ---------------------------------------------------------------------------


def _build_dongle_components(self_root: str, dongle_mac: str) -> dict:
    """Components for one physical USB dongle device.

    Each dongle gets its own HA device with connection state, scan, and
    remove buttons scoped to that specific dongle.

    Returns a fresh dict on every call.  To add a new per-dongle entity,
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
            "state_topic": f"{self_root}/dongle_{dongle_mac}/status",
        },
        "scan": {
            "platform": "button",
            "name": "Scan for sensor",
            "entity_category": "config",
            "command_topic": f"{self_root}/dongle_{dongle_mac}/scan",
            "payload_press": "scan",
        },
        "remove": {
            "platform": "button",
            "name": "Remove sensor",
            "entity_category": "config",
            "command_topic": f"{self_root}/dongle_{dongle_mac}/remove",
            "payload_press": "remove",
        },
    }


# ---------------------------------------------------------------------------
# Discovery schema migration
#
# Each entry in _MIGRATION_STEPS is a callable:
#   migrate_to_vN(client, cfg, logger, sensor_mac, sensor_type,
#                 dongle_mac, service_id, wait)
#
# The function clears *all* stale retained topics that were published by
# version N-1.  Adding a migration for a future version N:
#   1. Write def _migrate_to_vN(...)
#   2. Append it to _MIGRATION_STEPS
#   3. Bump DISCOVERY_SCHEMA_VERSION to N
#
# _MIGRATION_STEPS is indexed from 0; step at index i migrates from vi+1 to
# vi+2.  Currently there is only one step: v1 → v2.
# ---------------------------------------------------------------------------


def _migrate_to_v2(
    client: mqtt.Client,
    cfg: dict,
    logger: logging.Logger,
    sensor_mac: str,
    sensor_type: str,
    dongle_mac: str,
    service_id: str,
    wait: bool = True,
) -> None:
    """Clear all v1 retained topics for one sensor and the v1 bridge device.

    v1 sensor topics — legacy per-entity format:
      homeassistant/<platform>/wyzesense_<mac>/<entity>/config

    v1 bridge topic — 3.x identity:
      homeassistant/binary_sensor/wyzesense_bridge_<mac>/connection_state/config
    """
    # --- v1 sensor per-entity topics ---
    entity_types = ["signal_strength", "battery"]
    if sensor_type in BINARY_SENSOR_TYPES:
        entity_types.append("state")
        if sensor_type == "leak":
            entity_types.append("probe_state")
            entity_types.extend(["temperature", "humidity"])
    elif sensor_type == "keypad":
        entity_types.extend(["alarm_mode", "motion"])
    elif sensor_type == "chime":
        entity_types.extend(["play", "ring_id", "volume", "repeat_count"])
    else:
        entity_types.extend(["temperature", "humidity"])

    for entity_type in entity_types:
        if entity_type == "alarm_mode":
            platform = "alarm_control_panel"
        elif entity_type == "play":
            platform = "button"
        elif entity_type in ("ring_id", "volume", "repeat_count"):
            platform = "number"
        elif entity_type in ("state", "probe_state", "motion"):
            platform = "binary_sensor"
        else:
            platform = "sensor"
        topic = f"{cfg['hass_topic_root']}/{platform}/wyzesense_{sensor_mac}/{entity_type}/config"
        _publish(client, logger, topic, None, wait=wait)

    # --- v1 bridge topic (3.x identity) ---
    bridge_topic = f"{cfg['hass_topic_root']}/binary_sensor/wyzesense_bridge_{dongle_mac}/connection_state/config"
    _publish(client, logger, bridge_topic, None, wait=wait)


# List of migration step functions, one per schema version transition.
# Index 0 = migration to v2 (clears all pre-v2 topics).
_MIGRATION_STEPS: list[Callable] = [
    _migrate_to_v2,
]


# ---------------------------------------------------------------------------
# Low-level publish helper (module-level so migration steps can call it
# directly without needing a MqttGateway instance)
# ---------------------------------------------------------------------------


def _publish(
    client: mqtt.Client,
    logger: logging.Logger | None,
    topic: str,
    payload,
    is_json: bool = True,
    wait: bool = True,
    qos: int = _QOS_DATA,
    retain: bool = _RETAIN_DATA,
) -> mqtt.MQTTMessageInfo:
    """Publish a single MQTT message.

    Pass payload=None to clear a retained topic (publishes an empty payload).
    Defaults to data-topic settings (QoS 0, retain=False); callers override
    qos/retain for status, discovery, command, and number topics.
    """
    if payload is None:
        raw_payload = None
        # Clearing a retained topic requires retain=True
        retain = True
    elif is_json:
        raw_payload = json.dumps(payload)
    else:
        raw_payload = payload

    if logger:
        logger.debug(f"MQTT publish {topic=} {raw_payload=}")

    info = client.publish(topic, payload=raw_payload, qos=qos, retain=retain)
    if info.rc == mqtt.MQTT_ERR_SUCCESS:
        if wait:
            info.wait_for_publish(2)
    elif logger:
        logger.warning(f"MQTT publish error on {topic!r}: {mqtt.error_string(info.rc)}")
    return info


# ---------------------------------------------------------------------------
# Module-level sensor discovery cleanup helper
#
# Used by cli/mqtt_tool.py which operates without a MqttGateway instance.
# ---------------------------------------------------------------------------


def clear_sensor_discovery_topics(
    client: mqtt.Client,
    cfg: dict,
    logger: logging.Logger | None,
    sensor_mac: str,
    sensor_type: str,
    wait: bool = True,
) -> None:
    """Clear the sensor's discovery topic from every known schema version.

    Covers all versions so it is safe to call regardless of which schema
    version originally published the sensor's config topics.
    """
    # v1 per-entity sensor topics
    entity_types = ["signal_strength", "battery"]
    if sensor_type in BINARY_SENSOR_TYPES:
        entity_types.append("state")
        if sensor_type == "leak":
            entity_types.append("probe_state")
            entity_types.extend(["temperature", "humidity"])
    elif sensor_type == "keypad":
        entity_types.extend(["alarm_mode", "motion"])
    elif sensor_type == "chime":
        entity_types.extend(["play", "ring_id", "volume", "repeat_count"])
    else:
        entity_types.extend(["temperature", "humidity"])
    for entity_type in entity_types:
        if entity_type == "alarm_mode":
            platform = "alarm_control_panel"
        elif entity_type == "play":
            platform = "button"
        elif entity_type in ("ring_id", "volume", "repeat_count"):
            platform = "number"
        elif entity_type in ("state", "probe_state", "motion"):
            platform = "binary_sensor"
        else:
            platform = "sensor"
        topic = f"{cfg['hass_topic_root']}/{platform}/wyzesense_{sensor_mac}/{entity_type}/config"
        _publish(client, logger, topic, None, wait=wait)

    # v2 device-based sensor topic
    _publish(client, logger, f"{cfg['hass_topic_root']}/device/wyzesense_{sensor_mac}/config", None, wait=wait)


def clear_sensor_state_topics(
    client: mqtt.Client,
    cfg: dict,
    logger: logging.Logger | None,
    sensor_mac: str,
    sensor_type: str,
    wait: bool = True,
) -> None:
    """Clear all retained data and config-entity state topics for a sensor.

    Covers:
      - The sensor's primary data topic and status topic
      - Config entity state topics common to all sensors (sensor_name,
        device_class, invert_state, pin_count)
      - Chime number state topics (ring_id, volume, repeat_count) when
        sensor_type == "chime"

    Does NOT clear discovery topics — call ``clear_sensor_discovery_topics``
    for those.  Together they constitute a full sensor removal.
    """
    mac_topic = f"{cfg['self_topic_root']}/{sensor_mac}"
    _publish(client, logger, f"{mac_topic}/status", None, wait=wait, qos=_QOS_STATUS, retain=_RETAIN_STATUS)
    _publish(client, logger, mac_topic, None, wait=wait)
    for suffix in ["sensor_name", "device_class", "invert_state", "pin_count"]:
        _publish(client, logger, f"{mac_topic}/{suffix}", None, wait=wait, qos=_QOS_NUMBER, retain=_RETAIN_NUMBER)
    if sensor_type == "chime":
        for suffix in ["ring_id", "volume", "repeat_count"]:
            _publish(client, logger, f"{mac_topic}/{suffix}", None, wait=wait, qos=_QOS_NUMBER, retain=_RETAIN_NUMBER)


def clear_dongle_topics(
    client: mqtt.Client,
    cfg: dict,
    logger: logging.Logger | None,
    dongle_mac: str,
    wait: bool = True,
) -> None:
    """Clear all retained MQTT topics associated with a dongle device.

    Clears the dongle's HA discovery topic and its status topic.
    Does NOT clear sensor topics — call ``clear_sensor_state_topics`` and
    ``clear_sensor_discovery_topics`` for each sensor first.
    """
    _publish(
        client,
        logger,
        f"{cfg['hass_topic_root']}/device/ws2m_dongle_{dongle_mac}/config",
        None,
        wait=wait,
        qos=_QOS_DISCOVERY,
        retain=_RETAIN_DISCOVERY,
    )
    _publish(
        client,
        logger,
        f"{cfg['self_topic_root']}/dongle_{dongle_mac}/status",
        None,
        wait=wait,
        qos=_QOS_STATUS,
        retain=_RETAIN_STATUS,
    )
    if logger:
        logger.info(f"Cleared all dongle MQTT topics for {dongle_mac}")


# ---------------------------------------------------------------------------
# MqttGateway
# ---------------------------------------------------------------------------


class MqttGateway:
    """Owns the MQTT client connection and all publish/subscribe operations."""

    def __init__(self, cfg: dict, logger: logging.Logger | None = None):
        self._config = cfg
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

    def publish(
        self,
        topic: str,
        payload,
        is_json: bool = True,
        wait: bool = True,
        qos: int = _QOS_DATA,
        retain: bool = _RETAIN_DATA,
    ) -> mqtt.MQTTMessageInfo:
        """Publish to *topic*.  Pass payload=None to clear a retained topic."""
        return _publish(self._client, self._logger, topic, payload, is_json=is_json, wait=wait, qos=qos, retain=retain)

    # ------------------------------------------------------------------
    # Service discovery
    # ------------------------------------------------------------------

    def publish_service_discovery(self, service_id: str, log_level: str, wait: bool = True) -> None:
        """Publish the HA device-based discovery config for the ws2m service.

        Publishes a single retained topic:
          homeassistant/device/ws2m_service_<uuid>/config

        The service device is a software-only singleton representing this
        ws2m instance.  It intentionally omits hw_version and connections so
        that HA classifies it as a software device in the device info panel
        (HA's heuristic: sw_version present, hw_version and connections absent).
        To add a new service-level entity, edit _build_service_components().
        """
        cfg = self._config
        components = _build_service_components(cfg["self_topic_root"])
        for component in components.values():
            component["has_entity_name"] = True
            uid_key = component.get("command_topic", "").split("/")[-1]
            component["unique_id"] = f"ws2m_service_{service_id}_{uid_key}"

        payload = {
            "device": {
                "identifiers": [f"ws2m_service_{service_id}"],
                "manufacturer": "Raetha",
                "model": "WyzeSense2MQTT Service",
                "name": "WyzeSense2MQTT",
                "sw_version": VERSION,
            },
            "origin": {
                "name": "WyzeSense2MQTT",
                "sw_version": VERSION,
                "support_url": "https://github.com/raetha/wyzesense2mqtt",
            },
            "components": components,
            "qos": _QOS_DISCOVERY,
        }
        topic = f"{cfg['hass_topic_root']}/device/ws2m_service_{service_id}/config"
        self.publish(topic, payload, wait=wait, qos=_QOS_DISCOVERY, retain=_RETAIN_DISCOVERY)
        self._logger.debug(f"Published service discovery → {topic}")

        # Publish initial log_level state
        self.publish(
            f"{cfg['self_topic_root']}/log_level",
            log_level.upper(),
            is_json=False,
            wait=wait,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
        )

    # ------------------------------------------------------------------
    # Dongle discovery
    # ------------------------------------------------------------------

    def publish_dongle_discovery(
        self,
        dongle_mac: str,
        dongle_version: str,
        service_id: str,
        wait: bool = True,
    ) -> None:
        """Publish the HA device-based discovery config for one USB dongle.

        Publishes a single retained topic:
          homeassistant/device/ws2m_dongle_<mac>/config

        The dongle device is a hardware device with hw_version from the
        dongle firmware.  It is a child of the service device.
        To add a new per-dongle entity, edit _build_dongle_components().
        """
        cfg = self._config
        components = _build_dongle_components(cfg["self_topic_root"], dongle_mac)
        for key, component in components.items():
            component["has_entity_name"] = True
            component["unique_id"] = f"ws2m_dongle_{dongle_mac}_{key}"

        payload = {
            "device": {
                "hw_version": dongle_version,
                "identifiers": [f"ws2m_dongle_{dongle_mac}", dongle_mac],
                "manufacturer": "Raetha",
                "model": "WyzeSense Bridge Dongle",
                "name": f"WyzeSense Dongle {dongle_mac}",
                "sw_version": VERSION,
                "via_device": f"ws2m_service_{service_id}",
            },
            "origin": {
                "name": "WyzeSense2MQTT",
                "sw_version": VERSION,
                "support_url": "https://github.com/raetha/wyzesense2mqtt",
            },
            "components": components,
            "qos": _QOS_DISCOVERY,
        }
        topic = f"{cfg['hass_topic_root']}/device/ws2m_dongle_{dongle_mac}/config"
        self.publish(topic, payload, wait=wait, qos=_QOS_DISCOVERY, retain=_RETAIN_DISCOVERY)
        self._logger.debug(f"Published dongle discovery → {topic}")

    # ------------------------------------------------------------------
    # Sensor discovery
    # ------------------------------------------------------------------

    def publish_sensor_discovery(
        self,
        sensor_mac: str,
        sensor: dict,
        dongle_mac: str,
        service_id: str,
        sensor_online: bool,
        wait: bool = True,
        probe_available: bool = True,
    ) -> None:
        """Build and publish the device-based HA discovery payload for one sensor.

        Uses the v2 device-based format:
          homeassistant/device/wyzesense_<mac>/config

        Sensor availability depends on both the dongle and the service:
          - ws2m/<sensor_mac>/status  — sensor-level heartbeat timeout
          - ws2m/dongle_<mac>/status  — dongle connectivity

        The payload's ``components`` block is assembled from four sources:
          1. The sensor-type-specific builder from _COMPONENT_BUILDERS
          2. _build_diagnostic_components() — universal diagnostic entities
          3. _build_sensor_action_components() — universal action buttons
          4. _build_sensor_config_components() — name/class/invert controls

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
        remove_topic = f"{cfg['self_topic_root']}/dongle_{dongle_mac}/remove"

        # Merge all component sources — all return fresh dicts, safe to mutate below.
        # Pass probe_available to the leak builder so it can conditionally include probe_state.
        if sensor_type == "leak":
            components: dict = builder(sensor_mac, sensor, mac_topic, probe_available=probe_available)
        else:
            components: dict = builder(sensor_mac, sensor, mac_topic)
        components.update(_build_diagnostic_components())
        components.update(_build_sensor_action_components(sensor_mac, remove_topic))
        components.update(_build_sensor_config_components(sensor_mac, sensor, mac_topic))

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
                "via_device": f"ws2m_dongle_{dongle_mac}",
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
                {"topic": f"{cfg['self_topic_root']}/dongle_{dongle_mac}/status"},
            ],
            "availability_mode": "all",
            "qos": _QOS_DISCOVERY,
        }

        device_topic = f"{cfg['hass_topic_root']}/device/wyzesense_{sensor_mac}/config"
        self.publish(device_topic, device_payload, wait=wait, qos=_QOS_DISCOVERY, retain=_RETAIN_DISCOVERY)
        self._logger.debug(f"Published discovery for {sensor_mac} → {device_topic}")

        # Publish initial availability
        self.publish(
            f"{cfg['self_topic_root']}/{sensor_mac}/status",
            "online" if sensor_online else "offline",
            is_json=False,
            wait=wait,
            qos=_QOS_STATUS,
            retain=_RETAIN_STATUS,
        )

        # Publish initial config entity states
        self._publish_sensor_config_states(sensor_mac, sensor, wait=wait)

    def _publish_sensor_config_states(self, sensor_mac: str, sensor: dict, wait: bool = True) -> None:
        """Publish current config entity states for a sensor to their state topics.

        Called after discovery and after any config change so HA always shows
        the current value.
        """
        cfg = self._config
        mac_topic = f"{cfg['self_topic_root']}/{sensor_mac}"
        sensor_type = sensor.get("sensor_type", "unknown")

        # sensor_name
        self.publish(
            f"{mac_topic}/sensor_name",
            sensor.get("name", f"WyzeSense {sensor_mac}"),
            is_json=False,
            wait=wait,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
        )

        # device_class — only if this type has selectable options
        if sensor_type in DEVICE_CLASS_OPTIONS:
            current_class = sensor.get("class", SENSOR_TYPES.get(sensor_type, {}).get("device_class", ""))
            self.publish(
                f"{mac_topic}/device_class",
                current_class,
                is_json=False,
                wait=wait,
                qos=_QOS_NUMBER,
                retain=_RETAIN_NUMBER,
            )

        # invert_state — only for invertible sensor types
        if sensor_type in INVERTIBLE_SENSOR_TYPES:
            invert_val = "true" if sensor.get("invert_state") else "false"
            self.publish(
                f"{mac_topic}/invert_state",
                invert_val,
                is_json=False,
                wait=wait,
                qos=_QOS_NUMBER,
                retain=_RETAIN_NUMBER,
            )

        # pin_count — only for keypad
        if sensor_type == "keypad":
            pins = sensor.get("pins", [])
            if isinstance(pins, str):
                pins = [pins] if pins else []
            self.publish(
                f"{mac_topic}/pin_count",
                str(len(pins)),
                is_json=False,
                wait=wait,
                qos=_QOS_NUMBER,
                retain=_RETAIN_NUMBER,
            )

    # ------------------------------------------------------------------
    # Sensor topic cleanup
    # ------------------------------------------------------------------

    def clear_sensor_topics(self, sensor_mac: str, sensor_type: str, wait: bool = True) -> None:
        """Clear all retained topics for a sensor (used on removal)."""
        self._logger.info(f"Clearing MQTT topics for {sensor_mac}")
        clear_sensor_state_topics(self._client, self._config, self._logger, sensor_mac, sensor_type, wait=wait)
        clear_sensor_discovery_topics(self._client, self._config, self._logger, sensor_mac, sensor_type, wait=wait)

    def _clear_sensor_config_state_topics(self, sensor_mac: str, sensor_type: str, wait: bool = True) -> None:
        """Clear retained config entity state topics for a removed sensor."""
        mac_topic = f"{self._config['self_topic_root']}/{sensor_mac}"
        for suffix in ["sensor_name", "device_class", "invert_state", "pin_count"]:
            self.publish(f"{mac_topic}/{suffix}", None, wait=wait)
        if sensor_type == "chime":
            for suffix in ["ring_id", "volume", "repeat_count"]:
                self.publish(f"{mac_topic}/{suffix}", None, wait=wait)

    def clear_service_discovery(self, service_id: str, wait: bool = True) -> None:
        """Clear the retained service device discovery topic."""
        cfg = self._config
        topic = f"{cfg['hass_topic_root']}/device/ws2m_service_{service_id}/config"
        self.publish(topic, None, wait=wait)
        # Also clear the log_level state topic
        self.publish(f"{cfg['self_topic_root']}/log_level", None, wait=wait)
        self._logger.debug(f"Cleared service discovery → {topic}")

    def clear_dongle_discovery(self, dongle_mac: str, wait: bool = True) -> None:
        """Clear the retained dongle device discovery topic."""
        cfg = self._config
        topic = f"{cfg['hass_topic_root']}/device/ws2m_dongle_{dongle_mac}/config"
        self.publish(topic, None, wait=wait)
        self._logger.debug(f"Cleared dongle discovery → {topic}")

    def clear_dongle_all_topics(self, dongle_mac: str, wait: bool = True) -> None:
        """Clear all retained MQTT topics associated with a dongle device.

        Clears the dongle discovery topic and its status topic.  Does NOT
        clear sensor topics — callers should iterate sensors and call
        ``clear_sensor_topics()`` for each before calling this method.
        """
        clear_dongle_topics(self._client, self._config, self._logger, dongle_mac, wait=wait)

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
        service_id: str,
        from_version: int,
        wait: bool = True,
    ) -> None:
        """Clear stale retained topics for all schema versions from *from_version* to current.

        Each migration step handles sensor, dongle, and service topics for that
        version transition, driven by the DongleWorker's _init_sensors loop.
        """
        for step_index in range(from_version - 1, DISCOVERY_SCHEMA_VERSION - 1):
            if step_index < len(_MIGRATION_STEPS):
                target_version = step_index + 2
                self._logger.debug(f"Clearing v{target_version - 1} discovery topics for {sensor_mac}")
                _MIGRATION_STEPS[step_index](
                    self._client,
                    self._config,
                    self._logger,
                    sensor_mac,
                    sensor_type,
                    dongle_mac,
                    service_id,
                    wait=wait,
                )
