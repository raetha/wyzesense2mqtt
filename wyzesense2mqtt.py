''' 
Wyze Sense 2 MQTT
v0.5
'''
import json
import logging
import logging.config
import logging.handlers
import os
import socket
import sys
import subprocess
import yaml

import paho.mqtt.client as mqtt
import wyzesense
from retrying import retry

# Configuration File Locations
LOGGING_CONFIG_FILE = "config/logging.yaml"
GENERAL_CONFIG_FILE = "config/config.yaml"
SENSORS_CONFIG_FILE = "config/sensors.yaml"

# Read data from YAML file
def read_yaml_file(filename):
    try:
        with open(filename) as yaml_file:
            data = yaml.safe_load(yaml_file)
            return data
    except IOError as error:
        if LOGGER is None:
            print("File error: {0}".format(str(error)))
        else:
            LOGGER.error("File error: {0}".format(str(error)))

# Write data to YAML file
def write_yaml_file(filename, data):
    try:
        with open(filename, 'w') as yaml_file:
            yaml_file.write(yaml.safe_dump(data))
    except IOError as error:
        if LOGGER is None:
            print("File error: {0}".format(str(error)))
        else:
            LOGGER.error("File error: {0}".format(str(error)))

# Initialize logging
def init_logging():
    global LOGGER
    logging_config = read_yaml_file(LOGGING_CONFIG_FILE)
    try:
        log_path = os.path.dirname(logging_config['handlers']['file']['filename'])
        if not os.path.exists(log_path):
            os.makedirs(log_path)
    except:
        print("No logging file handler.")
    logging.config.dictConfig(logging_config)
    LOGGER = logging.getLogger("wyzesense2mqtt")
    LOGGER.debug("Logging initialized...")

# Initialize configuration
def init_config():
    global CONFIG
    LOGGER.debug("Reading configuration...")
    CONFIG = read_yaml_file(GENERAL_CONFIG_FILE)

# Initialize MQTT client connection
def init_mqtt_client():
    global MQTT_CLIENT, CONFIG

    # Configure MQTT Client
    MQTT_CLIENT = mqtt.Client(client_id = CONFIG['mqtt_client_id'], clean_session = CONFIG['mqtt_clean_session'])
    MQTT_CLIENT.username_pw_set(username = CONFIG['mqtt_username'], password = CONFIG['mqtt_password'])
    MQTT_CLIENT.reconnect_delay_set(min_delay=1, max_delay=120)
    MQTT_CLIENT.on_connect = on_connect
    MQTT_CLIENT.on_disconnect = on_disconnect
    MQTT_CLIENT.on_message = on_message

    # Connect to MQTT
    LOGGER.info("Attempting to open connection to MQTT host {0}".format(CONFIG['mqtt_host']))
    MQTT_CLIENT.connect(CONFIG['mqtt_host'], port = CONFIG['mqtt_port'], keepalive = CONFIG['mqtt_keepalive'])

# Initialize USB dongle
@retry(wait_exponential_multiplier=1000, wait_exponential_max=10000)
def init_wysesense_dongle():
    global WYSESENSE_DONGLE, CONFIG
    if CONFIG['usb_dongle'].lower() == "auto": 
        df = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode("utf-8").lower()
        for l in df.split("\n"):
            if ("e024" in l and "1a86" in l):
                for w in l.split(" "):
                    if ("hidraw" in w):
                        CONFIG['usb_dongle'] = "/dev/%s" % w
                        break

    LOGGER.info("Openning connection to dongle {0}".format(CONFIG['usb_dongle']))
    WYSESENSE_DONGLE = wyzesense.Open(CONFIG['usb_dongle'], on_event)
    LOGGER.debug("  MAC: {0}, VER: {1}, ENR: {2}".format(WYSESENSE_DONGLE.MAC, WYSESENSE_DONGLE.Version, WYSESENSE_DONGLE.ENR))

# Initialize sensor configuration
def init_sensors():
    global SENSORS
    LOGGER.debug("Reading sensors configuration...")
    if os.path.isfile(SENSORS_CONFIG_FILE):
        SENSORS = read_yaml_file(SENSORS_CONFIG_FILE)
    else:
        LOGGER.info("No sensors config file found.")

    for sensor_mac in SENSORS:
        if valid_sensor_mac(sensor_mac):
            send_discovery_topics(sensor_mac)

    # Check config against linked sensors
    result = WYSESENSE_DONGLE.List()
    LOGGER.debug("Linked sensors: {0}".format(result))
    if result:
        for sensor_mac in result:
            if valid_sensor_mac(sensor_mac):
                if SENSORS.get(sensor_mac) is None:
                    add_sensor_to_config(sensor_mac)
                    send_discovery_topics(sensor_mac)
    else:
        LOGGER.warning("Sensor list failed with result: {0}".format(result))

# Validate sensor MAC
def valid_sensor_mac(sensor_mac):
    if len(sensor_mac) == 8 and sensor_mac != "00000000" and sensor_mac != "\0\0\0\0\0\0\0\0" and sensor_mac != "\x00\x00\x00\x00\x00\x00\x00\x00":
        return True
    else:
        return False

# Add sensor to config
def add_sensor_to_config(sensor_mac, sensor_type, sensor_version):
    global SENSORS
    SENSORS[sensor_mac] = dict()
    SENSORS[sensor_mac]['name'] = "Wyze Sense {0}".format(sensor_mac)
    SENSORS[sensor_mac]['class'] = ("motion" if sensor_type == "motion" else "opening")
    if sensor_version is not None:
        SENSORS[sensor_mac]['sw_version'] = sensor_version

    LOGGER.info("Writing Sensors Config File")
    write_yaml_file(SENSORS_CONFIG_FILE, SENSORS)

# Send discovery topics
def send_discovery_topics(sensor_mac):
    global SENSORS, CONFIG
    LOGGER.info("Publishing discovery topics for {0}".format(sensor_mac))

    sensor_name = SENSORS[sensor_mac]['name']
    sensor_class = SENSORS[sensor_mac]['class']
    if SENSORS[sensor_mac].get('sw_version') is not None:
        sensor_version = SENSORS[sensor_mac]['sw_version']
    else:
        sensor_version = ""

    device_payload = {
        'identifiers': ["wyzesense_{0}".format(sensor_mac), sensor_mac],
        'manufacturer': "Wyze",
        'model': ("Sense Motion Sensor" if sensor_class == "motion" else "Sense Contact Sensor"),
        'name': sensor_name,
        'sw_version': sensor_version
    }

    entity_payloads = {
        'state': {
            'name': sensor_name,
            'dev_cla': sensor_class,
            'pl_on': "1",
            'pl_off': "0",
            'json_attr_t': CONFIG['wyzesense2mqtt_topic_root'] + sensor_mac
        },
        'signal_strength': {
            'name': "{0} Signal Strength".format(sensor_name),
            'dev_cla': "signal_strength",
            'unit_of_meas': "dBm"
        },
        'battery': {
            'name': "{0} Battery".format(sensor_name),
            'dev_cla': "battery",
            'unit_of_meas': "%"
        }
    }

    for entity in entity_payloads:
        entity_payloads[entity]['val_tpl'] = "{{{{ value_json.{0} }}}}".format(entity)
        entity_payloads[entity]['uniq_id'] = "wyzesense_{0}_{1}".format(sensor_mac, entity)
        entity_payloads[entity]['stat_t'] = CONFIG['wyzesense2mqtt_topic_root'] + sensor_mac
        entity_payloads[entity]['dev'] = device_payload
        sensor_type = ("binary_sensor" if entity == "state" else "sensor")

        entity_topic = "{0}{1}/wyzesense_{2}/{3}/config".format(CONFIG['hass_topic_root'], sensor_type, sensor_mac, entity)
        MQTT_CLIENT.publish(entity_topic, payload = json.dumps(entity_payloads[entity]), qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])
#        LOGGER.debug("  {0}".format(entity_topic))
#        LOGGER.debug("  {0}".format(json.dumps(entity_payloads[entity])))

# Clear any retained topics in MQTT
def clear_topics(sensor_mac):
    global CONFIG
    LOGGER.info("Clearing sensor topics")
    state_topic = "{0}{1}".format(CONFIG['wyzesense2mqtt_topic_root'], sensor_mac)
    MQTT_CLIENT.publish(state_topic, payload = None, qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])

    entity_types = ['state', 'signal_strength', 'battery']
    for entity_type in entity_types:
        sensor_type = ("binary_sensor" if entity_type == "state" else "sensor")
        entity_topic = "{0}{1}/wyzesense_{2}/{3}/config".format(CONFIG['hass_topic_root'], sensor_type, sensor_mac, entity_type)
        MQTT_CLIENT.publish(entity_topic, payload = None, qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])

def on_connect(MQTT_CLIENT, userdata, flags, rc):
    global CONFIG
    LOGGER.info("Connected to mqtt with result code {0}".format(str(rc)))
    MQTT_CLIENT.subscribe([(SCAN_TOPIC, CONFIG['mqtt_qos']), (REMOVE_TOPIC, CONFIG['mqtt_qos'])])
    MQTT_CLIENT.message_callback_add(SCAN_TOPIC, on_message_scan)
    MQTT_CLIENT.message_callback_add(REMOVE_TOPIC, on_message_remove)

def on_disconnect(MQTT_CLIENT, userdata, rc):
    LOGGER.info("Disconnected from mqtt with result code {0}".format(str(rc)))
    MQTT_CLIENT.message_callback_remove(SCAN_TOPIC)
    MQTT_CLIENT.message_callback_remove(REMOVE_TOPIC)

# Process messages
def on_message(MQTT_CLIENT, userdata, msg):
    LOGGER.info("{0}: {1}".format(msg.topic, str(msg.payload)))

# Process message to scan for new sensors
def on_message_scan(MQTT_CLIENT, userdata, msg):
    global SENSORS
    LOGGER.info("In on_message_scan: {0}".format(msg.payload.decode()))

    # TODO Switch scan to automatically add sensor and perform discovery
    # Testing new method
    result = WYSESENSE_DONGLE.Scan()
    LOGGER.debug("Scan result: {0}".format(result))

    if result:
        sensor_mac, sensor_type, sensor_version = result
        if valid_sensor_mac(sensor_mac):
            if SENSORS.get(sensor_mac) is None:
                add_sensor_to_config(sensor_mac, sensor_type, sensor_version)
                send_discovery_topics(sensor_mac)
        else:
            LOGGER.debug("Invalid sensor found: {0}".format(sensor_mac))
    else:
        LOGGER.debug("No new sensor found")

# Process message to remove sensor
def on_message_remove(MQTT_CLIENT, userdata, msg):
    LOGGER.info("In on_message_remove: {0}".format(msg.payload.decode()))
    sensor_mac = msg.payload.decode()

    if valid_sensor_mac(sensor_mac):
        WYSESENSE_DONGLE.Delete(sensor_mac)
        clear_topics(sensor_mac)
    else:
        LOGGER.debug("Invalid mac address: {0}".format(sensor_mac))

# Process event
def on_event(WYSESENSE_DONGLE, event):
    if valid_sensor_mac(event.MAC):
        if event.Type == "state":
            LOGGER.info("Processing state event for {0}".format(event.MAC))
            LOGGER.debug("Event data: {0}".format(event))
            (sensor_type, sensor_state, sensor_battery, sensor_signal) = event.Data
            event_payload = {
                'available': True,
                'mac': event.MAC,
                'state': (1 if sensor_state == "open" or sensor_state == "active" else 0),
                'device_class': ("motion" if sensor_type == "motion" else "opening"),
                'device_class_timestamp': event.Timestamp.isoformat(),
                'signal_strength': sensor_signal * -1,
                'battery': sensor_battery
            }
#            LOGGER.debug(event_payload)

            state_topic = "{0}{1}".format(CONFIG['wyzesense2mqtt_topic_root'], event.MAC)
            MQTT_CLIENT.publish(state_topic, payload = json.dumps(event_payload), qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])

            # Add sensor if it doesn't already exist
            if not event.MAC in SENSORS:
                add_sensor_to_config(event.MAC, sensor_type, None)
                send_discovery_topics(event.MAC)
        else:
            LOGGER.debug("Non-state event")
            LOGGER.debug("Event data: {0}".format(event))

    else:
        LOGGER.warning("Invalid MAC detected")
        LOGGER.warning("Event data: {0}".format(event))

# Initialize logging
init_logging()

# Initialize configuration
init_config()

# Set MQTT Topics
SCAN_TOPIC = "{0}scan".format(CONFIG['wyzesense2mqtt_topic_root'])
REMOVE_TOPIC = "{0}remove".format(CONFIG['wyzesense2mqtt_topic_root'])

# Initialize MQTT client connection
init_mqtt_client()

# Initialize USB dongle
init_wysesense_dongle()

# Initialize sensor configuration
init_sensors()

# MQTT client loop forever
MQTT_CLIENT.loop_forever(retry_first_connection = True)
