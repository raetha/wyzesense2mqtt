'''
WyzeSense to MQTT Gateway
'''
import json
import logging
import logging.config
import logging.handlers
import os
import shutil
import subprocess
import yaml

import paho.mqtt.client as mqtt
import wyzesense
from retrying import retry

# Configuration File Locations
CONFIG_PATH = "config/"
SAMPLES_PATH = "samples/"
MAIN_CONFIG_FILE = "config.yaml"
LOGGING_CONFIG_FILE = "logging.yaml"
SENSORS_CONFIG_FILE = "sensors.yaml"


# Read data from YAML file
def read_yaml_file(filename):
    try:
        with open(filename) as yaml_file:
            data = yaml.safe_load(yaml_file)
            return data
    except IOError as error:
        if (LOGGER is None):
            print(f"File error: {str(error)}")
        else:
            LOGGER.error(f"File error: {str(error)}")


# Write data to YAML file
def write_yaml_file(filename, data):
    try:
        with open(filename, 'w') as yaml_file:
            yaml_file.write(yaml.safe_dump(data))
    except IOError as error:
        if (LOGGER is None):
            print(f"File error: {str(error)}")
        else:
            LOGGER.error(f"File error: {str(error)}")


# Initialize logging
def init_logging():
    global LOGGER
    if (not os.path.isfile(CONFIG_PATH + LOGGING_CONFIG_FILE)):
        print("Copying default logging config file...")
        try:
            shutil.copy2(SAMPLES_PATH + LOGGING_CONFIG_FILE, CONFIG_PATH)
        except IOError as error:
            print(f"Unable to copy default logging config file. {str(error)}")
    logging_config = read_yaml_file(CONFIG_PATH + LOGGING_CONFIG_FILE)

    log_path = os.path.dirname(logging_config['handlers']['file']['filename'])
    try:
        if (not os.path.exists(log_path)):
            os.makedirs(log_path)
    except IOError:
        print("Unable to create log folder")
    logging.config.dictConfig(logging_config)
    LOGGER = logging.getLogger("wyzesense2mqtt")
    LOGGER.debug("Logging initialized...")


# Initialize configuration
def init_config():
    global CONFIG
    LOGGER.debug("Reading configuration...")
    if (not os.path.isfile(CONFIG_PATH + MAIN_CONFIG_FILE)):
        LOGGER.info("Copying default config file...")
        try:
            shutil.copy2(SAMPLES_PATH + MAIN_CONFIG_FILE, CONFIG_PATH)
        except IOError as error:
            LOGGER.error(f"Unable to copy default config file. {str(error)}")
    CONFIG = read_yaml_file(CONFIG_PATH + MAIN_CONFIG_FILE)


# Initialize MQTT client connection
def init_mqtt_client():
    global MQTT_CLIENT, CONFIG

    # Configure MQTT Client
    MQTT_CLIENT = mqtt.Client(
            client_id=CONFIG['mqtt_client_id'],
            clean_session=CONFIG['mqtt_clean_session']
    )
    MQTT_CLIENT.username_pw_set(
            username=CONFIG['mqtt_username'],
            password=CONFIG['mqtt_password']
    )
    MQTT_CLIENT.reconnect_delay_set(min_delay=1, max_delay=120)
    MQTT_CLIENT.on_connect = on_connect
    MQTT_CLIENT.on_disconnect = on_disconnect
    MQTT_CLIENT.on_message = on_message

    # Connect to MQTT
    LOGGER.info(f"Connecting to MQTT host {CONFIG['mqtt_host']}")
    MQTT_CLIENT.connect(
            CONFIG['mqtt_host'],
            port=CONFIG['mqtt_port'],
            keepalive=CONFIG['mqtt_keepalive']
    )


# Initialize USB dongle
@retry(wait_exponential_multiplier=1000, wait_exponential_max=10000)
def init_wyzesense_dongle():
    global WYZESENSE_DONGLE, CONFIG
    if (CONFIG['usb_dongle'].lower() == "auto"):
        device_list = subprocess.check_output(
                ["ls", "-la", "/sys/class/hidraw"]
        ).decode("utf-8").lower()
        for line in device_list.split("\n"):
            if (("e024" in line) and ("1a86" in line)):
                for device_name in line.split(" "):
                    if ("hidraw" in device_name):
                        CONFIG['usb_dongle'] = "/dev/%s" % device_name
                        break

    LOGGER.info(f"Connecting to dongle {CONFIG['usb_dongle']}")
    try:
        WYZESENSE_DONGLE = wyzesense.Open(CONFIG['usb_dongle'], on_event)
        LOGGER.debug(f"  MAC: {WYZESENSE_DONGLE.MAC},"
                     f"  VER: {WYZESENSE_DONGLE.Version},"
                     f"  ENR: {WYZESENSE_DONGLE.ENR}")
    except IOError:
        LOGGER.warning(f"No device found on path {CONFIG['usb_dongle']}")


# Initialize sensor configuration
def init_sensors():
    global SENSORS
    LOGGER.debug("Reading sensors configuration...")
    if (os.path.isfile(CONFIG_PATH + SENSORS_CONFIG_FILE)):
        SENSORS = read_yaml_file(CONFIG_PATH + SENSORS_CONFIG_FILE)
    else:
        LOGGER.info("No sensors config file found.")

    for sensor_mac in SENSORS:
        if (valid_sensor_mac(sensor_mac)):
            send_discovery_topics(sensor_mac)

    # Check config against linked sensors
    try:
        result = WYZESENSE_DONGLE.List()
        LOGGER.debug(f"Linked sensors: {result}")
        if (result):
            for sensor_mac in result:
                if (valid_sensor_mac(sensor_mac)):
                    if (SENSORS.get(sensor_mac) is None):
                        add_sensor_to_config(sensor_mac, None, None)
                        send_discovery_topics(sensor_mac)
        else:
            LOGGER.warning(f"Sensor list failed with result: {result}")
    except TimeoutError:
        pass


# Validate sensor MAC
def valid_sensor_mac(sensor_mac):
    LOGGER.debug(f"sensor_mac: {sensor_mac}")
    invalid_mac_list = [
            "00000000",
            "\0\0\0\0\0\0\0\0",
            "\x00\x00\x00\x00\x00\x00\x00\x00"
    ]
    if ((len(str(sensor_mac)) == 8) and (sensor_mac not in invalid_mac_list)):
        return True
    else:
        LOGGER.warning(f"Unpairing bad MAC: {sensor_mac}")
        try:
            WYZESENSE_DONGLE.Delete(sensor_mac)
            clear_topics(sensor_mac)
        except TimeoutError:
            pass
        return False


# Add sensor to config
def add_sensor_to_config(sensor_mac, sensor_type, sensor_version):
    global SENSORS
    SENSORS[sensor_mac] = dict()
    SENSORS[sensor_mac]['name'] = f"Wyze Sense {sensor_mac}"
    SENSORS[sensor_mac]['class'] = (
            "motion" if (sensor_type == "motion")
            else "opening"
    )
    SENSORS[sensor_mac]['invert_state'] = False
    if (sensor_version is not None):
        SENSORS[sensor_mac]['sw_version'] = sensor_version

    LOGGER.info("Writing Sensors Config File")
    write_yaml_file(CONFIG_PATH + SENSORS_CONFIG_FILE, SENSORS)


# Send discovery topics
def send_discovery_topics(sensor_mac):
    global SENSORS, CONFIG
    LOGGER.info(f"Publishing discovery topics for {sensor_mac}")

    sensor_name = SENSORS[sensor_mac]['name']
    sensor_class = SENSORS[sensor_mac]['class']
    if (SENSORS[sensor_mac].get('sw_version') is not None):
        sensor_version = SENSORS[sensor_mac]['sw_version']
    else:
        sensor_version = ""

    device_payload = {
        'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
        'manufacturer': "Wyze",
        'model': (
                "Sense Motion Sensor" if (sensor_class == "motion")
                else "Sense Contact Sensor"
        ),
        'name': sensor_name,
        'sw_version': sensor_version
    }

    entity_payloads = {
        'state': {
            'name': sensor_name,
            'dev_cla': sensor_class,
            'pl_on': "1",
            'pl_off': "0",
            'json_attr_t': f"{CONFIG['self_topic_root']}/{sensor_mac}"
        },
        'signal_strength': {
            'name': f"{sensor_name} Signal Strength",
            'dev_cla': "signal_strength",
            'unit_of_meas': "dBm"
        },
        'battery': {
            'name': f"{sensor_name} Battery",
            'dev_cla': "battery",
            'unit_of_meas': "%"
        }
    }

    for entity, entity_payload in entity_payloads.items():
        entity_payload['val_tpl'] = f"{{{{ value_json.{entity} }}}}"
        entity_payload['uniq_id'] = f"wyzesense_{sensor_mac}_{entity}"
        entity_payload['stat_t'] = \
            f"{CONFIG['self_topic_root']}/{sensor_mac}"
        entity_payload['dev'] = device_payload
        sensor_type = ("binary_sensor" if (entity == "state") else "sensor")

        entity_topic = f"{CONFIG['hass_topic_root']}/{sensor_type}/" \
                       f"wyzesense_{sensor_mac}/{entity}/config"
        MQTT_CLIENT.publish(
                entity_topic,
                payload=json.dumps(entity_payload),
                qos=CONFIG['mqtt_qos'],
                retain=CONFIG['mqtt_retain']
        )
        LOGGER.debug(f"  {entity_topic}")
        LOGGER.debug(f"  {json.dumps(entity_payload)}")


# Clear any retained topics in MQTT
def clear_topics(sensor_mac):
    global CONFIG
    LOGGER.info("Clearing sensor topics")
    state_topic = f"{CONFIG['self_topic_root']}/{sensor_mac}"
    MQTT_CLIENT.publish(
            state_topic,
            payload=None,
            qos=CONFIG['mqtt_qos'],
            retain=CONFIG['mqtt_retain']
    )

    entity_types = ['state', 'signal_strength', 'battery']
    for entity_type in entity_types:
        sensor_type = ("binary_sensor" if (entity_type == "state")
                       else "sensor")
        entity_topic = f"{CONFIG['hass_topic_root']}/{sensor_type}/" \
                       f"wyzesense_{sensor_mac}/{entity_type}/config"
        MQTT_CLIENT.publish(
                entity_topic,
                payload=None,
                qos=CONFIG['mqtt_qos'],
                retain=CONFIG['mqtt_retain']
        )


def on_connect(MQTT_CLIENT, userdata, flags, rc):
    global CONFIG
    if rc == 0:
        MQTT_CLIENT.subscribe(
                [(SCAN_TOPIC, CONFIG['mqtt_qos']),
                 (REMOVE_TOPIC, CONFIG['mqtt_qos']),
                 (RELOAD_TOPIC, CONFIG['mqtt_qos'])]
        )
        MQTT_CLIENT.message_callback_add(SCAN_TOPIC, on_message_scan)
        MQTT_CLIENT.message_callback_add(REMOVE_TOPIC, on_message_remove)
        MQTT_CLIENT.message_callback_add(RELOAD_TOPIC, on_message_reload)
        LOGGER.info(f"Connected to MQTT: return code {str(rc)}")
    elif rc == 3:
        LOGGER.warning(f"Connect to MQTT failed: server unavailable {str(rc)}")
    else:
        LOGGER.warning(f"Connect to MQTT failed: return code {str(rc)}")
        exit(1)


def on_disconnect(MQTT_CLIENT, userdata, rc):
    LOGGER.info(f"Disconnected from MQTT: return code {str(rc)}")
    MQTT_CLIENT.message_callback_remove(SCAN_TOPIC)
    MQTT_CLIENT.message_callback_remove(REMOVE_TOPIC)
    MQTT_CLIENT.message_callback_remove(RELOAD_TOPIC)


# Process messages
def on_message(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"{msg.topic}: {str(msg.payload)}")


# Process message to scan for new sensors
def on_message_scan(MQTT_CLIENT, userdata, msg):
    global SENSORS
    LOGGER.info(f"In on_message_scan: {msg.payload.decode()}")

    try:
        result = WYZESENSE_DONGLE.Scan()
        LOGGER.debug(f"Scan result: {result}")
        if (result):
            sensor_mac, sensor_type, sensor_version = result
            sensor_type = ("motion" if (sensor_type == 2) else "opening")
            if (valid_sensor_mac(sensor_mac)):
                if (SENSORS.get(sensor_mac)) is None:
                    add_sensor_to_config(
                            sensor_mac,
                            sensor_type,
                            sensor_version
                    )
                    send_discovery_topics(sensor_mac)
            else:
                LOGGER.debug(f"Invalid sensor found: {sensor_mac}")
        else:
            LOGGER.debug("No new sensor found")
    except TimeoutError:
        pass


# Process message to remove sensor
def on_message_remove(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"In on_message_remove: {msg.payload.decode()}")
    sensor_mac = msg.payload.decode()

    if (valid_sensor_mac(sensor_mac)):
        try:
            WYZESENSE_DONGLE.Delete(sensor_mac)
            clear_topics(sensor_mac)
        except TimeoutError:
            pass
    else:
        LOGGER.debug(f"Invalid mac address: {sensor_mac}")


# Process message to reload sensors
def on_message_reload(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"In on_message_reload: {msg.payload.decode()}")
    init_sensors()


# Process event
def on_event(WYZESENSE_DONGLE, event):
    global SENSORS
    if (valid_sensor_mac(event.MAC)):
        if (event.Type == "state"):
            LOGGER.info(f"State event data: {event}")
            (sensor_type, sensor_state, sensor_battery, sensor_signal) = event.Data

            # Add sensor if it doesn't already exist
            if (event.MAC not in SENSORS):
                add_sensor_to_config(event.MAC, sensor_type, None)
                send_discovery_topics(event.MAC)

            # Build event payload
            event_payload = {
                'available': True,
                'mac': event.MAC,
                'device_class': ("motion" if (sensor_type == "motion")
                                 else "opening"),
                'last_seen': event.Timestamp.timestamp(),
                'last_seen_iso': event.Timestamp.isoformat(),
                'signal_strength': sensor_signal * -1,
                'battery': sensor_battery
            }

            if (CONFIG.get('publish_sensor_name')):
                event_payload['name'] = SENSORS[event.MAC]['name']

            if (SENSORS[event.MAC].get('invert_state')):
                event_payload['state'] = (0 if (sensor_state == "open") or
                                               (sensor_state == "active")
                                          else 1)
            else:
                event_payload['state'] = (1 if (sensor_state == "open") or
                                               (sensor_state == "active")
                                          else 0)

            LOGGER.debug(event_payload)

            state_topic = f"{CONFIG['self_topic_root']}/{event.MAC}"
            MQTT_CLIENT.publish(
                    state_topic,
                    payload=json.dumps(event_payload),
                    qos=CONFIG['mqtt_qos'],
                    retain=CONFIG['mqtt_retain']
            )
        else:
            LOGGER.debug(f"Non-state event data: {event}")

    else:
        LOGGER.warning("!Invalid MAC detected!")
        LOGGER.warning(f"Event data: {event}")


# Initialize logging
init_logging()

# Initialize configuration
init_config()

# Set MQTT Topics
SCAN_TOPIC = f"{CONFIG['self_topic_root']}/scan"
REMOVE_TOPIC = f"{CONFIG['self_topic_root']}/remove"
RELOAD_TOPIC = f"{CONFIG['self_topic_root']}/reload"

# Initialize MQTT client connection
init_mqtt_client()

# Initialize USB dongle
init_wyzesense_dongle()

# Initialize sensor configuration
init_sensors()

# MQTT client loop forever
MQTT_CLIENT.loop_forever(retry_first_connection=True)
