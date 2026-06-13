#!/usr/bin/env python3
"""
WyzeSense to MQTT Gateway
"""

import json
import logging
import logging.config
import logging.handlers
import os
import shutil
import subprocess
import time

import mqtt_common
import paho.mqtt.client as mqtt
import wyzesense
from mqtt_common import (
    _BINARY_SENSORS,
    CONFIG_PATH,
    DISCOVERY_SCHEMA_VERSION,
    MAIN_CONFIG_FILE,
    SENSORS_CONFIG_FILE,
    WYZESENSE2MQTT_VERSION,
)
from retrying import retry

# Configuration File Locations (not shared with mqtt_common, since they're
# only relevant to the running bridge, not standalone CLI tools)
SAMPLES_PATH = "samples"
LOGGING_CONFIG_FILE = "logging.yaml"
SENSORS_STATE_FILE = "state.yaml"


# Simplify mapping of device classes.
# { **dict.fromkeys(['list', 'of', 'possible', 'identifiers'], 'device_class') }
DEVICE_CLASSES = {
    **dict.fromkeys([0x01, 0x0E, "switch", "switchv2"], "opening"),
    **dict.fromkeys([0x02, 0x0F, "motion", "motionv2"], "motion"),
    **dict.fromkeys([0x03, "leak"], "moisture"),
    **dict.fromkeys([0x07, "climate"], "temperature"),
}


# List of states that correlate to ON.
STATES_ON = ["active", "open", "wet"]

# Oldest state data that is considered fresh, older state data is stale and ignored
# 1 hour, converted to seconds
STALE_STATE = 1 * 60 * 60

# Keep persistant data about the sensors that isn't configurable in a seperate state variable
# Read/write this file to try and maintain a consistent state
SENSORS_STATE = {}


# V1 sensors send state every 4 hours, V2 sensors send every 2 hours
# For timeout of availability, use 2 times the report period to allow
# for one missed message. If 2 are missed, then the sensor probably is
# offline.
DEFAULT_V1_TIMEOUT_HOURS = 8
DEFAULT_V2_TIMEOUT_HOURS = 4

_DEVICE_MAPPING = {
    "motion": {
        "class": "motion",
        "on": "active",
        "off": "inactive",
        "model": "Wyze Sense V1 Motion Sensor",
        "hw_version": "V1",
        "timeout": DEFAULT_V1_TIMEOUT_HOURS,
    },
    "motionv2": {
        "class": "motion",
        "on": "active",
        "off": "inactive",
        "model": "Wyze Sense V2 Motion Sensor",
        "hw_version": "V2",
        "timeout": DEFAULT_V2_TIMEOUT_HOURS,
    },
    "switch": {
        "class": "opening",
        "on": "open",
        "off": "closed",
        "model": "Wyze Sense V1 Contact Sensor",
        "hw_version": "V1",
        "timeout": DEFAULT_V1_TIMEOUT_HOURS,
    },
    "switchv2": {
        "class": "opening",
        "on": "open",
        "off": "closed",
        "model": "Wyze Sense V2 Contact Sensor",
        "hw_version": "V2",
        "timeout": DEFAULT_V2_TIMEOUT_HOURS,
    },
    "leak": {
        "class": "moisture",
        "on": "wet",
        "off": "dry",
        "model": "Wyze Sense V2 Leak Sensor",
        "hw_version": "V2",
        "timeout": DEFAULT_V2_TIMEOUT_HOURS,
    },
    "climate": {
        "class": "temperature",
        "model": "Wyze Sense V2 Climate Sensor",
        "hw_version": "V2",
        "timeout": DEFAULT_V2_TIMEOUT_HOURS,
    },
    "unknown": {"timeout": DEFAULT_V1_TIMEOUT_HOURS},
}


# List of sw versions for V1 and V2 sensors, to determine which timeout to use by default
V1_SW = [19]
V2_SW = [23]

INITIALIZED = False


# Read data from YAML file
def read_yaml_file(filename):
    return mqtt_common.read_yaml_file(filename, LOGGER)


# Write data to YAML file
def write_yaml_file(filename, data):
    mqtt_common.write_yaml_file(filename, data, LOGGER)


# Initialize logging
def init_logging():
    global LOGGER
    if not os.path.isfile(os.path.join(CONFIG_PATH, LOGGING_CONFIG_FILE)):
        print("Copying default logging config file...")
        try:
            shutil.copy2(os.path.join(SAMPLES_PATH, LOGGING_CONFIG_FILE), CONFIG_PATH)
        except OSError as error:
            print(f"Unable to copy default logging config file. {str(error)}")
    logging_config = read_yaml_file(os.path.join(CONFIG_PATH, LOGGING_CONFIG_FILE))

    log_path = os.path.dirname(logging_config["handlers"]["file"]["filename"])
    try:
        if not os.path.exists(log_path):
            os.makedirs(log_path)
    except OSError:
        print("Unable to create log folder")
    logging.config.dictConfig(logging_config)
    LOGGER = logging.getLogger("wyzesense2mqtt")
    LOGGER.info("Logging initialized...")


# Initialize configuration
def init_config():
    global CONFIG
    LOGGER.info("Initializing configuration...")

    # Initialize CONFIG dictionary with default values, overridden by
    # config.yaml, overridden by environment variables.
    # Allows for addition of new settings and ensures that missing values will have a default at runtime
    CONFIG, config_from_file = mqtt_common.load_config(LOGGER)

    # fail on no config
    if CONFIG is None:
        LOGGER.error("Failed to load configuration, please configure.")
        exit(1)

    # write updated config file if needed
    if config_from_file is None or CONFIG != config_from_file:
        LOGGER.info("Writing updated config file")
        write_yaml_file(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE), CONFIG)


# Initialize MQTT client connection
def init_mqtt_client():
    global MQTT_CLIENT
    # Used for alternate MQTT connection method
    mqtt.Client.connected_flag = False

    # Configure MQTT Client
    MQTT_CLIENT = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=CONFIG["mqtt_client_id"], clean_session=CONFIG["mqtt_clean_session"]
    )
    MQTT_CLIENT.username_pw_set(username=CONFIG["mqtt_username"], password=CONFIG["mqtt_password"])
    MQTT_CLIENT.reconnect_delay_set(min_delay=1, max_delay=120)
    MQTT_CLIENT.on_connect = on_connect
    MQTT_CLIENT.on_disconnect = on_disconnect
    MQTT_CLIENT.on_message = on_message
    MQTT_CLIENT.enable_logger(LOGGER)

    # Connect to MQTT
    LOGGER.info(f"Connecting to MQTT host {CONFIG['mqtt_host']}")
    MQTT_CLIENT.connect_async(CONFIG["mqtt_host"], port=CONFIG["mqtt_port"], keepalive=CONFIG["mqtt_keepalive"])

    # Used for alternate MQTT connection method
    MQTT_CLIENT.loop_start()
    while not MQTT_CLIENT.connected_flag:
        time.sleep(1)

    # Make sure the service stays marked as offline until everything is initialized
    mqtt_publish(f"{CONFIG['self_topic_root']}/bridge_{WYZESENSE_DONGLE.MAC}/status", "offline", is_json=False)


# Retry forever on IO Error
def retry_if_io_error(exception):
    return isinstance(exception, IOError)


# Initialize USB dongle
@retry(wait_exponential_multiplier=1000, wait_exponential_max=30000, retry_on_exception=retry_if_io_error)
def init_wyzesense_dongle():
    global WYZESENSE_DONGLE
    if CONFIG["usb_dongle"].lower() == "auto":
        device_list = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode("utf-8").lower()
        for line in device_list.split("\n"):
            if ("e024" in line) and ("1a86" in line):
                for device_name in line.split(" "):
                    if "hidraw" in device_name:
                        CONFIG["usb_dongle"] = f"/dev/{device_name}"
                        break

    LOGGER.info(f"Connecting to dongle {CONFIG['usb_dongle']}")
    try:
        WYZESENSE_DONGLE = wyzesense.Open(CONFIG["usb_dongle"], on_event, LOGGER)
        LOGGER.info(
            f"Dongle {CONFIG['usb_dongle']}: "
            f"[ MAC: {WYZESENSE_DONGLE.MAC}, VER: {WYZESENSE_DONGLE.Version}, ENR: {WYZESENSE_DONGLE.ENR}]"
        )
    except OSError as error:
        LOGGER.error(f"No device found on path {CONFIG['usb_dongle']}: {str(error)}")


# Initialize bridge discovery
def init_bridge_discovery(wait=True):
    if CONFIG["hass_discovery"]:
        connection_state_payload = {
            "default_entity_id": "binary_sensor.wyzesense2mqtt_bridge_connection_state",
            "device": {
                "hw_version": f"{WYZESENSE_DONGLE.Version}",
                "identifiers": [f"wyzesense2mqtt_bridge_{WYZESENSE_DONGLE.MAC}"],
                "manufacturer": "Raetha",
                "model": "Bridge",
                "name": f"WyzeSense2MQTT Bridge {WYZESENSE_DONGLE.MAC}",
                "sw_version": f"{WYZESENSE2MQTT_VERSION}",
            },
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "has_entity_name": True,
            "name": "Connection state",
            "object_id": "wyzesense2mqtt_bridge_connection_state",
            "origin": {
                "name": "WyzeSense2MQTT",
                "sw_version": f"{WYZESENSE2MQTT_VERSION}",
                "support_url": "https://github.com/raetha/wyzesense2mqtt",
            },
            "payload_off": "offline",
            "payload_on": "online",
            "qos": CONFIG["mqtt_qos"],
            "state_topic": f"{CONFIG['self_topic_root']}/bridge_{WYZESENSE_DONGLE.MAC}/status",
            "unique_id": f"wyzesense2mqtt_bridge_{WYZESENSE_DONGLE.MAC}_connection_state",
        }
        mqtt_publish(
            f"{CONFIG['hass_topic_root']}/binary_sensor/wyzesense_bridge_{WYZESENSE_DONGLE.MAC}/connection_state/config",
            connection_state_payload,
            wait=wait,
        )


# Initialize sensor configuration
def init_sensors(wait=True):
    # Initialize sensor dictionary
    global SENSORS, SENSORS_STATE
    SENSORS = {}

    # Load config file
    if os.path.isfile(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)):
        LOGGER.info("Reading sensors configuration...")
        SENSORS = read_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE))
        sensors_config_file_found = True
    else:
        LOGGER.warning("No sensors config file found.")
        sensors_config_file_found = False

    # Add invert_state value if missing
    for sensor_mac in SENSORS:
        if SENSORS[sensor_mac].get("invert_state") is None:
            SENSORS[sensor_mac]["invert_state"] = False

    # Load previous known states
    if os.path.isfile(os.path.join(CONFIG_PATH, SENSORS_STATE_FILE)):
        LOGGER.info("Reading sensors last known state...")
        SENSORS_STATE = read_yaml_file(os.path.join(CONFIG_PATH, SENSORS_STATE_FILE))
        if SENSORS_STATE.get("modified") is not None:
            if (time.time() - SENSORS_STATE["modified"]) > STALE_STATE:
                LOGGER.warning("Ignoring stale state data")
                SENSORS_STATE = {}
            else:
                # Remove this field so we don't get a bogus warning below
                del SENSORS_STATE["modified"]

    # Check config against linked sensors
    checked_linked = False
    try:
        LOGGER.info("Checking sensors against dongle list...")
        result = WYZESENSE_DONGLE.List()
        if result:
            checked_linked = True

            for sensor_mac in result:
                if valid_sensor_mac(sensor_mac):
                    if SENSORS.get(sensor_mac) is None:
                        add_sensor_to_config(sensor_mac)
                        LOGGER.warning(
                            f"Linked sensor with mac {sensor_mac} automatically added to sensors configuration"
                        )
                        LOGGER.warning(
                            f"Please update sensor configuration file "
                            f"{os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)} restart the service/reload the sensors"
                        )

                    # If not a configured sensor, then adding it will also add it to the state
                    # So only check if in the state if it is a configured sensor
                    elif SENSORS_STATE.get(sensor_mac) is None:
                        # Only track state for linked sensors
                        # If it wasn't configured, it'd get added above, including in state
                        # Intialize last seen time to now and start online
                        SENSORS_STATE[sensor_mac] = {"last_seen": time.time(), "online": True}

            # We could save sensor state for sensors that aren't linked to the dongle if we fail
            # to check, then add a configured sensor to the state which gets written on stop. The
            # Next run it'll add the bad state mac. So to help with that, when we do check the
            # linked sensors, we should also remove anything in the state that wasn't linked
            delete = [sensor_mac for sensor_mac in SENSORS_STATE if sensor_mac not in result]
            for sensor_mac in delete:
                del SENSORS_STATE[sensor_mac]
                LOGGER.info(f"Removed unlinked sensor ({sensor_mac}) from state")

        else:
            LOGGER.warning(f"Sensor list failed with result: {result}")

    except TimeoutError:
        LOGGER.error("Dongle list timeout")
        pass

    if not checked_linked:
        # Unable to get linked sensors
        # Make sure all configured sensors have a state
        for sensor_mac in SENSORS:
            if SENSORS_STATE.get(sensor_mac) is None:
                # Intialize last seen time to now and start online
                SENSORS_STATE[sensor_mac] = {"last_seen": time.time(), "online": True}

    # Save sensors file if didn't exist
    if not sensors_config_file_found:
        LOGGER.info("Writing Sensors Config File")
        write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)

    # One-time migration of discovery topics if the schema has changed since
    # the last run (e.g. clears stale per-entity topics after upgrading to
    # device-based discovery). See DISCOVERY_SCHEMA_VERSION above.
    if CONFIG["hass_discovery"]:
        recorded_version = get_discovery_schema_version()
        if recorded_version < DISCOVERY_SCHEMA_VERSION:
            LOGGER.info(
                f"Migrating MQTT discovery topics from schema v{recorded_version} to v{DISCOVERY_SCHEMA_VERSION}..."
            )
            for sensor_mac in SENSORS_STATE:
                if valid_sensor_mac(sensor_mac):
                    sensor_type = SENSORS.get(sensor_mac, {}).get("sensor_type", "unknown")
                    migrate_discovery_topics(sensor_mac, sensor_type, recorded_version, wait=wait)
            set_discovery_schema_version(DISCOVERY_SCHEMA_VERSION)

    # Send discovery topics
    if CONFIG["hass_discovery"]:
        for sensor_mac in SENSORS_STATE:
            if valid_sensor_mac(sensor_mac):
                send_discovery_topics(sensor_mac, wait=wait)


# Validate sensor MAC
def valid_sensor_mac(sensor_mac):
    invalid_mac_list = ["00000000", "\0\0\0\0\0\0\0\0", "\x00\x00\x00\x00\x00\x00\x00\x00"]

    if (len(str(sensor_mac)) == 8) and (sensor_mac not in invalid_mac_list):
        return True
    else:
        LOGGER.warning(f"Unpairing bad MAC: {sensor_mac}")
        try:
            WYZESENSE_DONGLE.Delete(sensor_mac)
            clear_topics(sensor_mac)
        except TimeoutError:
            LOGGER.error("Timeout removing bad mac")
    return False


# Add sensor to config
def add_sensor_to_config(sensor_mac, sensor_type=None, sensor_version=None):
    LOGGER.info(f"Adding sensor to config: {sensor_mac}")
    SENSORS[sensor_mac] = {"name": f"WyzeSense {sensor_mac}"}
    if sensor_type:
        SENSORS[sensor_mac]["sensor_type"] = sensor_type
        if sensor_type in DEVICE_CLASSES:
            SENSORS[sensor_mac]["class"] = DEVICE_CLASSES[sensor_type]

    if sensor_version:
        SENSORS[sensor_mac]["sw_version"] = sensor_version

    # Intialize last seen time to now and start online
    SENSORS_STATE[sensor_mac] = {"last_seen": time.time(), "online": True}

    write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)


# Delete sensor from config
def delete_sensor_from_config(sensor_mac):
    LOGGER.info(f"Deleting sensor from config: {sensor_mac}")
    try:
        del SENSORS[sensor_mac]
        write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)
        del SENSORS_STATE[sensor_mac]
    except KeyError:
        LOGGER.error(f"{sensor_mac} not found in SENSORS")


# Publish MQTT topic
def mqtt_publish(mqtt_topic, mqtt_payload, is_json=True, wait=True):
    return mqtt_common.mqtt_publish(MQTT_CLIENT, CONFIG, LOGGER, mqtt_topic, mqtt_payload, is_json, wait)


# Send discovery topics
#
# Uses the HA "device-based" MQTT discovery format (single config topic per
# device, with each entity defined under "components"). This format has been
# supported since HA 2024.4 and is the recommended approach as of the
# HA 2026.6 MQTT integration docs. See docs/HA_MQTT_COMPLIANCE.md for the
# verified HA/MQTT version and notes for future maintenance.
def send_discovery_topics(sensor_mac, wait=True):
    LOGGER.info(f"Publishing discovery topics for {sensor_mac}")

    sensor = SENSORS[sensor_mac]
    sensor_type = sensor.get("sensor_type", "unknown")
    if sensor_type not in _DEVICE_MAPPING:
        LOGGER.error(f"Unsupported sensor type: {sensor_type}")
        return

    attr = {"sw_version": "unknown", "mac": "sensor_mac"}

    attr.update(_DEVICE_MAPPING[sensor_type])
    attr.update(sensor)

    mac_topic = f"{CONFIG['self_topic_root']}/{sensor_mac}"

    components = {}
    if sensor_type in _BINARY_SENSORS:
        components["state"] = {
            "platform": "binary_sensor",
            "name": None,
            "device_class": attr["class"],
            "payload_on": attr["on"],
            "payload_off": attr["off"],
            "json_attributes_topic": mac_topic,
        }

        # Extra payloads for leak sensor
        if sensor_type == "leak":
            components["probe_state"] = {
                "platform": "binary_sensor",
                "name": "Extension probe",
                "device_class": attr["class"],
                "payload_on": attr["on"],
                "payload_off": attr["off"],
                "json_attributes_topic": mac_topic,
            }

            # Leak sensors report temperature in Celsius and humidity
            components["temperature"] = {
                "platform": "sensor",
                "name": None,
                "device_class": "temperature",
                "state_class": "measurement",
                "unit_of_measurement": "°C",  # Leak sensors report in Celsius
                "suggested_display_precision": 1,
                "json_attributes_topic": mac_topic,
            }

            components["humidity"] = {
                "platform": "sensor",
                "name": None,
                "device_class": "humidity",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "suggested_display_precision": 0,
                "json_attributes_topic": mac_topic,
            }

    elif sensor_type == "climate":
        components["temperature"] = {
            "platform": "sensor",
            "name": None,
            "device_class": "temperature",
            "state_class": "measurement",
            "unit_of_measurement": "°F",
            "suggested_display_precision": 1,
            "json_attributes_topic": mac_topic,
        }

        components["humidity"] = {
            "platform": "sensor",
            "name": None,
            "device_class": "humidity",
            "state_class": "measurement",
            "unit_of_measurement": "%",
            "suggested_display_precision": 0,
            "json_attributes_topic": mac_topic,
        }
    else:
        LOGGER.error(f"Unexpected sensor type: {sensor_type}")
        return

    # Common payloads for sensors
    components["signal_strength"] = {
        "platform": "sensor",
        "name": None,
        "device_class": "signal_strength",
        "state_class": "measurement",
        "unit_of_measurement": "dBm",
        "suggested_display_precision": 0,
        "entity_category": "diagnostic",
    }
    components["battery"] = {
        "platform": "sensor",
        "name": None,
        "device_class": "battery",
        "state_class": "measurement",
        "unit_of_measurement": "%",
        "suggested_display_precision": 0,
        "entity_category": "diagnostic",
    }

    availability_topics = [
        {"topic": f"{CONFIG['self_topic_root']}/{sensor_mac}/status"},
        {"topic": f"{CONFIG['self_topic_root']}/bridge_{WYZESENSE_DONGLE.MAC}/status"},
    ]

    for entity, component_payload in components.items():
        component_payload["unique_id"] = f"wyzesense_{sensor_mac}_{entity}"
        component_payload["has_entity_name"] = True
        component_payload["value_template"] = f"{{{{ value_json.{entity} }}}}"

    device_payload = {
        "device": {
            "identifiers": [f"wyzesense_{sensor_mac}", sensor_mac],
            "manufacturer": "WyzeLabs",
            "model": attr["model"],
            "hw_version": attr["hw_version"],
            "name": attr["name"],
            "sw_version": attr["sw_version"],
            "via_device": f"wyzesense2mqtt_bridge_{WYZESENSE_DONGLE.MAC}",
        },
        # "name" and "sw_version" identify this as a wyzesense2mqtt-managed
        # discovery payload and which discovery schema it uses. HA ignores
        # these extra top-level keys, but `wyzesense2mqtt_cli.py
        # cleanup-discovery` and any future migration tooling can use them to
        # find/identify our retained config topics on the broker, including
        # ones for sensors no longer present in sensors.yaml.
        "origin": {
            "name": "WyzeSense2MQTT",
            "sw_version": f"{WYZESENSE2MQTT_VERSION}",
            "support_url": "https://github.com/raetha/wyzesense2mqtt",
        },
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "components": components,
        "state_topic": mac_topic,
        "availability": availability_topics,
        "availability_mode": "all",
        "qos": CONFIG["mqtt_qos"],
    }

    device_topic = f"{CONFIG['hass_topic_root']}/device/wyzesense_{sensor_mac}/config"
    mqtt_publish(device_topic, device_payload, wait=wait)

    LOGGER.info(f"  {device_topic}")
    LOGGER.info(f"  {json.dumps(device_payload)}")

    mqtt_publish(
        f"{CONFIG['self_topic_root']}/{sensor_mac}/status",
        "online" if SENSORS_STATE[sensor_mac]["online"] else "offline",
        is_json=False,
        wait=wait,
    )


# --- Discovery schema migrations -------------------------------------------
#
# On startup, init_sensors() compares the schema version recorded in
# migrations.yaml to DISCOVERY_SCHEMA_VERSION (from mqtt_common). For each
# version in between (inclusive of the old, exclusive of the new), the
# matching cleaner is run once per known sensor, then the recorded version is
# updated. This makes cleanup of stale/duplicate discovery topics automatic
# on upgrade, without needing to re-clear topics that have already been
# migrated. See mqtt_common.py for the version history and cleaner functions.
#
# Sensors that are no longer in sensors.yaml at all (e.g. removed by editing
# the file by hand) aren't covered by this; use
# `python3 wyzesense2mqtt_cli.py cleanup-discovery` for those.
def get_discovery_schema_version():
    return mqtt_common.get_discovery_schema_version(LOGGER)


def set_discovery_schema_version(version):
    mqtt_common.set_discovery_schema_version(version, LOGGER)


def migrate_discovery_topics(sensor_mac, sensor_type, from_version, wait=True):
    mqtt_common.migrate_discovery_topics(MQTT_CLIENT, CONFIG, LOGGER, sensor_mac, sensor_type, from_version, wait=wait)


# Clear any retained topics in MQTT
def clear_topics(sensor_mac, wait=True):
    LOGGER.info("Clearing sensor topics")
    mqtt_publish(f"{CONFIG['self_topic_root']}/{sensor_mac}/status", None, wait=wait)
    mqtt_publish(f"{CONFIG['self_topic_root']}/{sensor_mac}", None, wait=wait)

    # clear discovery topics if configured
    if CONFIG["hass_discovery"]:
        sensor = SENSORS.get(sensor_mac, {})
        sensor_type = sensor.get("sensor_type", "unknown")

        # Clear discovery topics from every known schema version, not just
        # the current one, so a full sensor removal doesn't leave stale
        # entities behind regardless of which version originally published
        # the discovery config.
        mqtt_common.clear_all_discovery_topics(MQTT_CLIENT, CONFIG, LOGGER, sensor_mac, sensor_type, wait=wait)


def on_connect(MQTT_CLIENT, userdata, flags, reason_code, properties):
    if reason_code == 0:
        MQTT_CLIENT.subscribe(
            [(SCAN_TOPIC, CONFIG["mqtt_qos"]), (REMOVE_TOPIC, CONFIG["mqtt_qos"]), (RELOAD_TOPIC, CONFIG["mqtt_qos"])]
        )
        MQTT_CLIENT.message_callback_add(SCAN_TOPIC, on_message_scan)
        MQTT_CLIENT.message_callback_add(REMOVE_TOPIC, on_message_remove)
        MQTT_CLIENT.message_callback_add(RELOAD_TOPIC, on_message_reload)
        MQTT_CLIENT.connected_flag = True
        LOGGER.info(f"Connected to MQTT: {reason_code}")
        # We are in a mqtt callback, so can not wait for new messages to publish
        mqtt_publish(
            f"{CONFIG['self_topic_root']}/bridge_{WYZESENSE_DONGLE.MAC}/status", "online", is_json=False, wait=False
        )
    else:
        LOGGER.warning(f"Connection to MQTT failed: {reason_code}")


def on_disconnect(MQTT_CLIENT, userdata, flags, reason_code, properties):
    MQTT_CLIENT.message_callback_remove(SCAN_TOPIC)
    MQTT_CLIENT.message_callback_remove(REMOVE_TOPIC)
    MQTT_CLIENT.message_callback_remove(RELOAD_TOPIC)
    MQTT_CLIENT.connected_flag = False
    LOGGER.info(f"Disconnected from MQTT: {reason_code}")


# We don't handle any additional messages from MQTT, just log them
def on_message(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"{msg.topic}: {str(msg.payload)}")


# Process message to scan for new sensors
def on_message_scan(MQTT_CLIENT, userdata, msg):
    result = None
    LOGGER.info(f"In on_message_scan: {msg.payload.decode()}")

    # The scan will do a couple additional calls even after the new sensor is found
    # These calls may time out, so catch it early so we can still add the sensor properly
    try:
        result = WYZESENSE_DONGLE.Scan()
    except TimeoutError:
        pass

    if result:
        LOGGER.info(f"Scan result: {result}")
        sensor_mac, sensor_type, sensor_version = result
        if valid_sensor_mac(sensor_mac):
            if (SENSORS.get(sensor_mac)) is None:
                add_sensor_to_config(sensor_mac, sensor_type, sensor_version)
                if CONFIG["hass_discovery"]:
                    # We are in a mqtt callback, so can not wait for new messages to publish
                    send_discovery_topics(sensor_mac, wait=False)
        else:
            LOGGER.info(f"Invalid sensor found: {sensor_mac}")
    else:
        LOGGER.info("No new sensor found")


# Process message to remove sensor
def on_message_remove(MQTT_CLIENT, userdata, msg):
    sensor_mac = msg.payload.decode()
    LOGGER.info(f"In on_message_remove: {sensor_mac}")

    if valid_sensor_mac(sensor_mac):
        # Deleting from the dongle may timeout, but we still need to do
        # the rest so catch it early
        try:
            WYZESENSE_DONGLE.Delete(sensor_mac)
        except TimeoutError:
            pass
        # We are in a mqtt callback so cannot wait for new messages to publish
        clear_topics(sensor_mac, wait=False)
        delete_sensor_from_config(sensor_mac)
    else:
        LOGGER.info(f"Invalid mac address: {sensor_mac}")


# Process message to reload sensors
def on_message_reload(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"In on_message_reload: {msg.payload.decode()}")

    # Save off the last known state so we don't overwrite new state by re-reading the previously saved file
    LOGGER.info("Writing Sensors State File")
    write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_STATE_FILE), SENSORS_STATE)

    # We are in a mqtt callback so cannot wait for new messages to publish
    init_sensors(wait=False)


# Process event
def on_event(WYZESENSE_DONGLE, event):
    if not INITIALIZED:
        return

    LOGGER.info(f"State event data: {event}")
    if not valid_sensor_mac(event.mac):
        LOGGER.warning("!Invalid MAC detected")
        return

    if valid_sensor_mac(event.mac):
        if event.mac not in SENSORS:
            add_sensor_to_config(event.mac, event.sensor_type)
            if CONFIG["hass_discovery"]:
                send_discovery_topics(event.mac)
            LOGGER.warning(f"Linked sensor with mac {event.mac} automatically added to sensors configuration")
            LOGGER.warning(
                f"Please update sensor configuration file "
                f"{os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)} restart the service/reload the sensors"
            )
            s = SENSORS[event.mac]
        else:
            s = SENSORS[event.mac]
            old_type = s.get("sensor_type", "unknown")
            if event.sensor_type != old_type:
                LOGGER.info("Updating Sensors Config File")
                s["sensor_type"] = event.sensor_type
                write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)
                if CONFIG["hass_discovery"]:
                    send_discovery_topics(event.mac)
                LOGGER.warning(f"Linked sensor with mac {event.mac} automatically added to sensors configuration")
                LOGGER.warning(
                    f"Please update sensor configuration file "
                    f"{os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)} restart the service/reload the sensors"
                )

        # Store last seen time for availability
        SENSORS_STATE[event.mac]["last_seen"] = event.timestamp

        mqtt_publish(f"{CONFIG['self_topic_root']}/{event.mac}/status", "online", is_json=False)

        # Set back online if it was offline
        if not SENSORS_STATE[event.mac]["online"]:
            SENSORS_STATE[event.mac]["online"] = True
            LOGGER.info(f"{event.mac} is back online!")

        if event.event not in ("alarm", "status"):
            LOGGER.info(f"Unknown event: {event}")
            return

        payload = {}
        payload.update(s)
        payload.update(vars(event))
        # Surfaced as entity attributes (via json_attributes_topic) so a user
        # can see in HA which discovery schema a device's entities were
        # published with - useful for spotting devices that haven't picked
        # up a newer schema for some reason.
        payload["wyzesense2mqtt_version"] = WYZESENSE2MQTT_VERSION
        payload["discovery_schema_version"] = DISCOVERY_SCHEMA_VERSION

        LOGGER.info(f"{CONFIG['self_topic_root']}/{event.mac}")
        LOGGER.info(payload)
        mqtt_publish(f"{CONFIG['self_topic_root']}/{event.mac}", payload)
    else:
        LOGGER.warning("!Invalid MAC detected!")
        LOGGER.warning(f"Event data: {event}")


def Stop():
    mqtt_publish(f"{CONFIG['self_topic_root']}/bridge_{WYZESENSE_DONGLE.MAC}/status", "offline", is_json=False)

    # Stop the dongle first, letting this thread finish anything it might be busy doing, like handling an event
    WYZESENSE_DONGLE.Stop()

    # All event handling should now be done, close the mqtt connection
    MQTT_CLIENT.loop_stop()
    MQTT_CLIENT.disconnect()

    # Save off the last known state
    LOGGER.info("Writing Sensors State File")
    SENSORS_STATE["modified"] = time.time()
    write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_STATE_FILE), SENSORS_STATE)

    LOGGER.info("********************************** Wyzesense2mqtt stopped ***********************************")


if __name__ == "__main__":
    # Initialize logging
    init_logging()

    print("********************************** Wyzesense2mqtt starting **********************************")

    # Initialize configuration
    init_config()

    # Set MQTT Topics
    SCAN_TOPIC = f"{CONFIG['self_topic_root']}/scan"
    REMOVE_TOPIC = f"{CONFIG['self_topic_root']}/remove"
    RELOAD_TOPIC = f"{CONFIG['self_topic_root']}/reload"

    # Initialize USB dongle
    init_wyzesense_dongle()

    # Initialize MQTT client connection
    init_mqtt_client()

    # Initialize bridge discovery topics
    init_bridge_discovery()

    # Initialize sensor configuration
    init_sensors()

    # All initialized now, so set the flag to allow message event to be processed
    INITIALIZED = True

    # And mark the service as online
    mqtt_publish(f"{CONFIG['self_topic_root']}/bridge_{WYZESENSE_DONGLE.MAC}/status", "online", is_json=False)

    # Loop forever until keyboard interrupt or SIGINT
    try:
        while True:
            time.sleep(5)
            # Check if there is any exceptions in the dongle thread
            WYZESENSE_DONGLE.CheckError()

            if not MQTT_CLIENT.connected_flag:
                mqtt_publish(
                    f"{CONFIG['self_topic_root']}/bridge_{WYZESENSE_DONGLE.MAC}/status", "offline", is_json=False
                )

            # Check for availability of the devices
            now = time.time()
            for mac in SENSORS_STATE:
                if SENSORS_STATE[mac]["online"]:
                    LOGGER.debug(f"Checking availability of {mac}")

                    sensor = SENSORS[mac]

                    sensor_type = sensor.get("sensor_type", "unknown")
                    # First the sensor type to decide the timeout value
                    timeout = _DEVICE_MAPPING[sensor_type]["timeout"] * 60 * 60

                    # Then check if the device has its own timeout value
                    timeout = sensor.get("timeout", timeout)

                    if (now - SENSORS_STATE[mac]["last_seen"]) > timeout:
                        mqtt_publish(f"{CONFIG['self_topic_root']}/{mac}/status", "offline", is_json=False)
                        LOGGER.warning(f"{mac} has gone offline!")
                        SENSORS_STATE[mac]["online"] = False
    except KeyboardInterrupt:
        LOGGER.warning("User interrupted")
    except Exception:
        LOGGER.error("An error occurred", exc_info=True)
    finally:
        Stop()
