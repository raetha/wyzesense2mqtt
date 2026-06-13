"""
Shared helpers for wyzesense2mqtt's MQTT discovery handling.

This module has no dependency on the WyzeSense dongle and is intended to be
imported by both the main server (wyzesense2mqtt.py) and standalone CLI
tools (e.g. wyzesense2mqtt_cli.py) that need to talk to the same MQTT broker
using the same config/topic conventions, without running the bridge itself.
"""

import json
import os

import paho.mqtt.client as mqtt
import yaml

WYZESENSE2MQTT_VERSION = "3.1"

# Configuration File Locations
CONFIG_PATH = "config"
MAIN_CONFIG_FILE = "config.yaml"
SENSORS_CONFIG_FILE = "sensors.yaml"
MIGRATIONS_STATE_FILE = "migrations.yaml"

# Sensor types whose primary entity is a binary_sensor rather than a sensor
_BINARY_SENSORS = ("motion", "motionv2", "switch", "switchv2", "leak")

# Default configuration values. Used to seed config.yaml on first run and to
# fill in any keys missing from an existing config.yaml/env.
DEFAULT_CONFIG = {
    "mqtt_host": None,
    "mqtt_port": 1883,
    "mqtt_username": None,
    "mqtt_password": None,
    "mqtt_client_id": "wyzesense2mqtt",
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


# Read data from a YAML file
def read_yaml_file(filename, logger=None):
    try:
        with open(filename) as yaml_file:
            return yaml.safe_load(yaml_file)
    except OSError as error:
        if logger is None:
            print(f"File error: {error}")
        else:
            logger.error(f"File error: {error}")
        return None


# Write data to a YAML file
def write_yaml_file(filename, data, logger=None):
    try:
        with open(filename, "w") as yaml_file:
            yaml_file.write(yaml.safe_dump(data))
    except OSError as error:
        if logger is None:
            print(f"File error: {error}")
        else:
            logger.error(f"File error: {error}")


# Load configuration: defaults, overridden by config.yaml, overridden by env.
# Returns (config, config_from_file) - config_from_file is None if no config
# file existed yet, which callers can use to decide whether to write one out.
def load_config(logger=None):
    config = dict(DEFAULT_CONFIG)

    config_from_file = None
    config_path = os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE)
    if os.path.isfile(config_path):
        config_from_file = read_yaml_file(config_path, logger)
        if config_from_file:
            config.update(config_from_file)

    for key, value in os.environ.items():
        key = str(key).lower()
        if key in config:
            if value.isnumeric():
                value = int(value)
            elif value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            elif value.lower() == "none":
                value = None
            config[key] = value

    return config, config_from_file


# Publish an MQTT topic using an already-connected client
def mqtt_publish(client, config, logger, mqtt_topic, mqtt_payload, is_json=True, wait=True):
    payload = json.dumps(mqtt_payload) if is_json else mqtt_payload
    if logger:
        logger.debug(f"Publishing, {mqtt_topic=}, {payload=}")
    mqtt_message_info = client.publish(
        mqtt_topic, payload=payload, qos=config["mqtt_qos"], retain=config["mqtt_retain"]
    )
    if mqtt_message_info.rc == mqtt.MQTT_ERR_SUCCESS:
        if wait:
            mqtt_message_info.wait_for_publish(2)
    elif logger:
        logger.warning(f"MQTT publish error: {mqtt.error_string(mqtt_message_info.rc)}")
    return mqtt_message_info


# --- Discovery schema migrations --------------------------------------------
#
# DISCOVERY_SCHEMA_VERSION identifies the shape of the MQTT discovery
# payloads this version of wyzesense2mqtt publishes. Bump this any time the
# discovery topic structure changes (e.g. a future HA MQTT change requires a
# different topic layout or payload shape), and add a corresponding entry to
# _DISCOVERY_CLEANERS for the version being moved *away from*, containing a
# function that clears whatever retained topics that old version published.
#
#   1 -> legacy per-entity discovery topics
#        (homeassistant/<component>/wyzesense_<mac>/<entity>/config)
#   2 -> current device-based discovery topic
#        (homeassistant/device/wyzesense_<mac>/config)
DISCOVERY_SCHEMA_VERSION = 2


# Clear legacy (schema v1) per-entity discovery config topics for a sensor
def _clear_discovery_topics_v1(client, config, logger, sensor_mac, sensor_type, wait=True):
    entity_types = ["signal_strength", "battery"]
    if sensor_type in _BINARY_SENSORS:
        entity_types.append("state")
        if sensor_type == "leak":
            entity_types.append("probe_state")
            entity_types.extend(["temperature", "humidity"])
    else:
        entity_types.extend(["temperature", "humidity"])

    for entity_type in entity_types:
        component = "binary_sensor" if entity_type in ("state", "probe_state") else "sensor"
        mqtt_publish(
            client,
            config,
            logger,
            f"{config['hass_topic_root']}/{component}/wyzesense_{sensor_mac}/{entity_type}/config",
            None,
            wait=wait,
        )


# Clear current (schema v2) device-based discovery config topic for a sensor
def _clear_discovery_topics_v2(client, config, logger, sensor_mac, sensor_type, wait=True):
    mqtt_publish(
        client, config, logger, f"{config['hass_topic_root']}/device/wyzesense_{sensor_mac}/config", None, wait=wait
    )


# Map of "version being migrated away from" -> cleaner function.
# When bumping DISCOVERY_SCHEMA_VERSION, add the cleaner for the version that
# is being replaced (it should clear whatever topics that version published).
_DISCOVERY_CLEANERS = {
    1: _clear_discovery_topics_v1,
    2: _clear_discovery_topics_v2,
}


# Read the recorded discovery schema version. Installs predating this
# tracking file are assumed to be on the original per-entity (v1) format.
def get_discovery_schema_version(logger=None):
    migrations_path = os.path.join(CONFIG_PATH, MIGRATIONS_STATE_FILE)
    if os.path.isfile(migrations_path):
        data = read_yaml_file(migrations_path, logger) or {}
        return data.get("discovery_schema_version", 1)
    return 1


# Persist the discovery schema version after migrations have run
def set_discovery_schema_version(version, logger=None):
    write_yaml_file(os.path.join(CONFIG_PATH, MIGRATIONS_STATE_FILE), {"discovery_schema_version": version}, logger)


# Run any pending discovery topic migrations for a single sensor, clearing
# retained topics from every schema version between `from_version`
# (inclusive) and DISCOVERY_SCHEMA_VERSION (exclusive).
def migrate_discovery_topics(client, config, logger, sensor_mac, sensor_type, from_version, wait=True):
    for version in range(from_version, DISCOVERY_SCHEMA_VERSION):
        cleaner = _DISCOVERY_CLEANERS.get(version)
        if cleaner:
            if logger:
                logger.debug(f"Clearing v{version} discovery topics for {sensor_mac}")
            cleaner(client, config, logger, sensor_mac, sensor_type, wait=wait)


# Clear discovery topics from every known schema version for a sensor (used
# on full sensor removal, so it doesn't matter which version originally
# published the discovery config).
def clear_all_discovery_topics(client, config, logger, sensor_mac, sensor_type, wait=True):
    for cleaner in _DISCOVERY_CLEANERS.values():
        cleaner(client, config, logger, sensor_mac, sensor_type, wait=wait)
