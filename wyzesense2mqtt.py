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
from retrying import retry
from wyzesense_custom import *

# Configuration File Locations
LOGGING_CONFIG_FILE = "config/logging.yaml"
GENERAL_CONFIG_FILE = "config/config.yaml"
DEVICES_CONFIG_FILE = "config/devices.yaml"

# Read data from YAML file
def read_yaml_file(filename):
    global _LOGGER
    try:
        with open(filename) as yaml_file:
            data = yaml.safe_load(yaml_file)
            return data
    except IOError as error:
        if _LOGGER is None:
            print("File error: {0}".format(str(error)))
        else:
            _LOGGER.error("File error: {0}".format(str(error)))

# Write data to YAML file
def write_yaml_file(filename, data):
    global _LOGGER
    try:
        with open(filename, 'w') as yaml_file:
            yaml_file.write(yaml.safe_dump(data))
    except IOError as error:
        if _LOGGER is None:
            print("File error: {0}".format(str(error)))
        else:
            _LOGGER.error("File error: {0}".format(str(error)))

# Initialize logging
def init_logging(LOGGING_CONFIG_FILE):
    global _LOGGER
    logging_config = read_yaml_file(LOGGING_CONFIG_FILE)
    try:
        log_path = os.path.dirname(logging_config['handlers']['file']['filename'])
        if not os.path.exists(log_path):
            os.makedirs(log_path)
    except:
        print("No logging file handler.")
    logging.config.dictConfig(logging_config)
    _LOGGER = logging.getLogger("wyzesense2mqtt")

# Initialize Config
def init_config(GENERAL_CONFIG_FILE):
    global CONFIG
    CONFIG = read_yaml_file(GENERAL_CONFIG_FILE)

# Initialize Devices
def init_devices(DEVICES_CONFIG_FILE):
    global DEVICES
    try:
        DEVICES = read_yaml_file(DEVICES_CONFIG_FILE)
        for device in DEVICES:
            send_discovery_topics(device, DEVICES[device]['name'], DEVICES[device]['class'])
    except:
        _LOGGER.info("No devices config file found.")
        
# Initialize USB Dongle
def init_usb_dongle():
    global ws
    if CONFIG['usb_dongle'].lower() == "auto": 
        df = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode("utf-8").lower()
        for l in df.split("\n"):
            if ("e024" in l and "1a86" in l):
                for w in l.split(" "):
                    if ("hidraw" in w):
                        usb_dongle = "/dev/%s" % w
    else:
        usb_dongle = CONFIG['usb_dongle']
    _LOGGER.info("Attempting to open connection to hub at {0}".format(usb_dongle))
    ws = beginConn()

# Send HASS discovery topics
#TODO FIX THIS WITH ACCEPTING MAC, NAME, CLASS
def send_discovery_topics(sensor_mac, sensor_type):
    global DEVICES, DEVICES_CONFIG_FILE
    _LOGGER.info("Publishing discovery topics")

    # Capture new device
    if DEVICES.get(sensor_mac) is None:
        DEVICES[sensor_mac] = dict()
        DEVICES[sensor_mac]['name'] = "Wyze Sense {0}".format(sensor_mac)
        DEVICES[sensor_mac]['class'] = ("motion" if sensor_type == "motion" else "opening")
        write_yaml_file(DEVICES_CONFIG_FILE, DEVICES)

    device_name = DEVICES[sensor_mac]['name']
    device_class = DEVICES[sensor_mac]['class']

    device_payload = {
        'identifiers': ["wyzesense_{0}".format(sensor_mac), sensor_mac],
        'manufacturer': "Wyze",
        'model': ("Sense Motion Sensor" if sensor_type == "motion" else "Sense Contact Sensor"),
        'name': device_name
    }

    entities = {
        'state': {
            'name': device_name,
            'dev_cla': device_class,
            'pl_on': "1",
            'pl_off': "0",
            'json_attr_t': CONFIG['wyzesense2mqtt_topic_root'] + sensor_mac
        },
        'signal_strength': {
            'name': "{0} Signal Strength".format(device_name),
            'dev_cla': "signal_strength",
            'unit_of_meas': "dBm"
        },
        'battery': {
            'name': "{0} Battery".format(device_name),
            'dev_cla': "battery",
            'unit_of_meas': "%"
        }
    }

    # Send Discovery Topics
    for entity in entities :
        entities[entity]['val_tpl'] = "{{{{ value_json.{0} }}}}".format(entity)
        entities[entity]['uniq_id'] = "wyzesense_{0}_{1}".format(sensor_mac, entity)
        entities[entity]['stat_t'] = CONFIG['wyzesense2mqtt_topic_root'] + sensor_mac
        entities[entity]['dev'] = device_payload
        sensor_type = ("binary_sensor" if entity == "state" else "sensor")

        entity_topic = "{0}{1}/wyzesense_{2}_{3}/config".format(CONFIG['hass_topic_root'], sensor_type, sensor_mac, entity)
        client.publish(entity_topic, payload = json.dumps(entities[entity]), qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])
        _LOGGER.info("  {0}".format(entity_topic))
        _LOGGER.debug("  {0}".format(json.dumps(entities[entity])))

# Clear any retained topics in MQTT
def clear_retained_mqtt_topics(sensor_mac):
    _LOGGER.info("Clearing device topics")
    event_topic = "{0}{1}".format(CONFIG['wyzesense2mqtt_topic_root'], sensor_mac)
    client.publish(event_topic, payload = None, qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])

    binary_sensor_topic = "{0}binary_sensor/wyzesense_{1}/config".format(CONFIG['hass_topic_root'], sensor_mac)
    client.publish(binary_sensor_topic, payload = None, qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])

    signal_strength_sensor_topic = "{0}sensor/wyzesense_{1}_signal_strength/config".format(CONFIG['hass_topic_root'], sensor_mac)
    client.publish(signal_strength_sensor_topic, payload = None, qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])

    battery_sensor_topic = "{0}sensor/wyzesense_{1}_battery/config".format(CONFIG['hass_topic_root'], sensor_mac)
    client.publish(battery_sensor_topic, payload = None, qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])

def on_connect(client, userdata, flags, rc):
    _LOGGER.info("Connected with result code {0}".format(str(rc)))
    client.subscribe([(SCAN_TOPIC, CONFIG['mqtt_qos']), (REMOVE_TOPIC, CONFIG['mqtt_qos'])])
    client.message_callback_add(SCAN_TOPIC, on_message_scan)
    client.message_callback_add(REMOVE_TOPIC, on_message_remove)

def on_disconnect(client, userdata, rc):
    _LOGGER.info("Disconnected with result code {0}".format(str(rc)))
    client.message_callback_remove(SCAN_TOPIC)
    client.message_callback_remove(REMOVE_TOPIC)

# Process messages
def on_message(client, userdata, msg):
    _LOGGER.info("{0} {1}".format(msg.topic, str(msg.payload)))

# Process message to scan for new devices
def on_message_scan(client, userdata, msg):
    _LOGGER.info("In on_message_scan: {0}".format(msg.payload.decode()))
    prescan_result = ws.List()
    _LOGGER.debug("Result of prescan {0}".format(prescan_result))   

    ws.Scan()

    postscan_result = ws.List()
    _LOGGER.debug("Result of postscan {0}".format(postscan_result))  

    s = diff(postscan_result, prescan_result)  
    _LOGGER.info("Diff is {0}".format(s))  

    if s != []:
        jsonData = json.dumps({'macs': s})
        _LOGGER.debug(jsonData)
        client.publish(SCAN_RESULT_TOPIC, payload = jsonData)
    else:
        _LOGGER.debug("Empty Scan")

# Process message to remove device
def on_message_remove(client, userdata, msg):
    _LOGGER.info("In on_message_remove: {0}".format(msg.payload.decode()))
    sensor_mac = msg.payload

    ws.Delete(sensor_mac)

    clear_retained_mqtt_topics(sensor_mac)

def on_event(ws, event):
    _LOGGER.info("Processing Event")
    _LOGGER.debug("Event data: {0}".format(event))
    if event.Type == "state":
        try:
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

            _LOGGER.debug(event_payload)

            event_topic = "{0}{1}".format(CONFIG['wyzesense2mqtt_topic_root'], event.MAC)
            client.publish(event_topic, payload = json.dumps(event_payload), qos = CONFIG['mqtt_qos'], retain = CONFIG['mqtt_retain'])

            send_discovery_topics(event.MAC, sensor_type)

        except TimeoutError as err:
            _LOGGER.error(err)
        except socket.timeout as err:            
            _LOGGER.error(err)
        except: # catch *all* exceptions
            e = sys.exc_info()[0]
            _LOGGER.error("Error: {0}".format(e))

@retry(wait_exponential_multiplier=1000, wait_exponential_max=10000)
def beginConn():
    return Open(CONFIG['usb_dongle'], on_event)

# Initialize Configuration
init_logging(LOGGING_CONFIG_FILE)
init_config(GENERAL_CONFIG_FILE)
init_devices(DEVICES_CONFIG_FILE)
init_usb_dongle()

# Set MQTT Topics
SCAN_TOPIC = "{0}scan".format(CONFIG['wyzesense2mqtt_topic_root'])
SCAN_RESULT_TOPIC = "{0}scan_result".format(CONFIG['wyzesense2mqtt_topic_root'])
REMOVE_TOPIC = "{0}remove".format(CONFIG['wyzesense2mqtt_topic_root'])

diff = lambda l1, l2: [x for x in l1 if x not in l2]

# Configure MQTT Client
client = mqtt.Client(client_id = CONFIG['mqtt_client_id'], clean_session = CONFIG['mqtt_clean_session'])
client.username_pw_set(username = CONFIG['mqtt_username'], password = CONFIG['mqtt_password'])
client.reconnect_delay_set(min_delay=1, max_delay=120)
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message

# Connect to MQTT and maintain connection
_LOGGER.info("Attempting to open connection to MQTT host {0}".format(CONFIG['mqtt_host']))
client.connect(CONFIG['mqtt_host'], port = CONFIG['mqtt_port'], keepalive = CONFIG['mqtt_keepalive'])
client.loop_forever(retry_first_connection = True)
