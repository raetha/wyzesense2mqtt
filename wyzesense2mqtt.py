import paho.mqtt.client as mqtt
import json
import socket
import sys
import logging
from retrying import retry
from wyzesense_custom import *

# Read configuration file
CONFIG_FILE = "config/config.json"
def read_config():
    with open(CONFIG_FILE) as config_file:
        data = json.load(config_file)
    return data
config = read_config()

# Set Config File Variables
MQTT_HOST = config["mqtt"]["host"]
MQTT_PORT = config["mqtt"]["port"]
MQTT_USERNAME = config["mqtt"]["username"]
MQTT_PASSWORD = config["mqtt"]["password"]
MQTT_CLIENT_ID = config["mqtt"]["client_id"]
MQTT_CLEAN_SESSION = config["mqtt"]["clean_session"]
MQTT_KEEPALIVE = config["mqtt"]["keepalive"]
MQTT_QOS = config["mqtt"]["qos"]
MQTT_RETAIN = config["mqtt"]["retain"]
PERFORM_HASS_DISCOVERY = config["perform_hass_discovery"]
HASS_TOPIC_ROOT = config["hass_topic_root"]
WYZESENSE2MQTT_TOPIC_ROOT = config["wyzesense2mqtt_topic_root"]
USB_DEVICE = config["usb_device"]
LOGGING_FILENAME = config["logging"]["filename"]
LOGGING_FILEMODE = config["logging"]["filemode"]
LOGGING_FORMAT = config["logging"]["format"]
LOGGING_DATEFMT = config["logging"]["datefmt"]

# Set MQTT Topics
SCAN_TOPIC = WYZESENSE2MQTT_TOPIC_ROOT + "scan"
SCAN_RESULT_TOPIC = WYZESENSE2MQTT_TOPIC_ROOT + "scan_result"
REMOVE_TOPIC = WYZESENSE2MQTT_TOPIC_ROOT + "remove"

diff = lambda l1, l2: [x for x in l1 if x not in l2]

def init_logging():
    # set up logging to file
    logging.basicConfig(level=logging.DEBUG, format=LOGGING_FORMAT, datefmt=LOGGING_DATEFMT, filename=LOGGING_FILENAME, filemode=LOGGING_FILEMODE)
    # define a Handler which writes INFO messages or higher to the sys.stderr
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    # set a format which is simpler for console use
    formatter = logging.Formatter("%(message)s")
    console.setFormatter(formatter)
    # add the handler to the root logger
    logging.getLogger('').addHandler(console)
    global LOG
    LOG = logging.getLogger("wyzesense2mqtt")

def on_connect(client, userdata, flags, rc):
    LOG.debug("Connected with result code " + str(rc))
    client.subscribe([(SCAN_TOPIC, MQTT_QOS), (REMOVE_TOPIC, MQTT_QOS)])
    client.message_callback_add(SCAN_TOPIC, on_message_scan)
    client.message_callback_add(REMOVE_TOPIC, on_message_remove)
    LOG.debug("Handlers Added")

def on_disconnect(client, userdata, rc):
    LOG.debug("Disconnected with result code " + str(rc))
    client.message_callback_remove(SCAN_TOPIC)
    client.message_callback_remove(REMOVE_TOPIC)
    LOG.debug("Handlers Removed")

def on_message(client, userdata, msg):
    LOG.debug(msg.topic+" " + str(msg.payload))  

def on_message_scan(client, userdata, msg):
    LOG.debug("In on_message_scan: {0}".format(msg.payload.decode()))
    prescan_result = ws.List()
    LOG.debug("Result of prescan {0}".format(prescan_result))   

    ws.Scan()

    postscan_result = ws.List()
    LOG.debug("Result of postscan {0}".format(postscan_result))  

    s = diff(postscan_result, prescan_result)  
    LOG.debug("Diff is {0}".format(s))  

    if s != []:
        jsonData = json.dumps({"macs": s})
        LOG.debug(jsonData)
        client.publish(SCAN_RESULT_TOPIC, payload = jsonData)
    else:
        LOG.debug("Empty Scan")

def on_message_remove(client, userdata, msg):
    LOG.debug(msg.topic+" " + str(msg.payload))
    sensor_mac = msg.payload

    ws.Delete(sensor_mac)

    clear_retained_mqtt_topics(sensor_mac)

# Send HASS discovery topics to MQTT
def send_discovery_topics(sensor_mac, sensor_type):
    device_payload = {
        "identifiers": ["wyzesense_{0}".format(sensor_mac)],
        "manufacturer": "Wyze",
        "model": "Motion Sensor" if sensor_type == "motion" else "Contact Sensor",
        "name": "Wyze Sense Motion Sensor" if sensor_type == "motion" else "Wyze Sense Contact Sensor"
    }

    binary_sensor_payload = {
        "device": device_payload,
        "name": "Wyze Sense {0}".format(sensor_mac),
        "unique_id": "wyzesense_{0}".format(sensor_mac),
        "device_class": "motion" if sensor_type == "motion" else "opening",
        "state_topic": WYZESENSE2MQTT_TOPIC_ROOT+sensor_mac,
        "value_template": "{{ value_json.state }}",
        "payload_off": "0",
        "payload_on": "1"
    }
    binary_sensor_topic = HASS_TOPIC_ROOT+"binary_sensor/wyzesense_{0}/config".format(sensor_mac)
    client.publish(binary_sensor_topic, payload = json.dumps(binary_sensor_payload), qos = MQTT_QOS, retain = MQTT_RETAIN)

    signal_strength_sensor_payload = {
        "device": device_payload,
        "name": "Wyze Sense {0} Signal Strength".format(sensor_mac),
        "unique_id": "wyzesense_{0}_signal_strength".format(sensor_mac),
        "device_class": "signal_strength",
        "state_topic": WYZESENSE2MQTT_TOPIC_ROOT+sensor_mac,
        "value_template": "{{ value_json.signal_strength }}",
        "unit_of_measurement": "dBm"
    }
    signal_strength_sensor_topic = HASS_TOPIC_ROOT+"sensor/wyzesense_{0}_signal_strength/config".format(sensor_mac)
    client.publish(signal_strength_sensor_topic, payload = json.dumps(signal_strength_sensor_payload), qos = MQTT_QOS, retain = MQTT_RETAIN)

    battery_sensor_payload = {
        "device": device_payload,
        "name": "Wyze Sense {0} Battery".format(sensor_mac),
        "unique_id": "wyzesense_{0}_battery".format(sensor_mac),
        "device_class": "battery",
        "state_topic": WYZESENSE2MQTT_TOPIC_ROOT+sensor_mac,
        "value_template": "{{ value_json.battery_level }}",
        "unit_of_measurement": "%"
    }
    battery_sensor_topic = HASS_TOPIC_ROOT+"sensor/wyzesense_{0}_battery/config".format(sensor_mac)
    client.publish(battery_sensor_topic, payload = json.dumps(battery_sensor_payload), qos = MQTT_QOS, retain = MQTT_RETAIN)

# Clear any retained topics in MQTT
def clear_retained_mqtt_topics(sensor_mac):
    event_topic = WYZESENSE2MQTT_TOPIC_ROOT+"{0}".format(event.MAC)
    client.publish(event_topic, payload = None, qos = MQTT_QOS, retain = MQTT_RETAIN)

    binary_sensor_topic = HASS_TOPIC_ROOT+"binary_sensor/wyzesense_{0}/config".format(sensor_mac)
    client.publish(binary_sensor_topic, payload = None, qos = MQTT_QOS, retain = MQTT_RETAIN)

    signal_strength_sensor_topic = HASS_TOPIC_ROOT+"sensor/wyzesense_{0}_signal_strength/config".format(sensor_mac)
    client.publish(signal_strength_sensor_topic, payload = None, qos = MQTT_QOS, retain = MQTT_RETAIN)

    battery_sensor_topic = HASS_TOPIC_ROOT+"sensor/wyzesense_{0}_battery/config".format(sensor_mac)
    client.publish(battery_sensor_topic, payload = None, qos = MQTT_QOS, retain = MQTT_RETAIN)

def on_event(ws, event):
    LOG.debug("In Event")
    if event.Type == 'state':
        try:
            (sensor_type, sensor_state, sensor_battery, sensor_signal) = event.Data
            event_payload = {
                "available": True,
                "mac": event.MAC,
                "state": 1 if sensor_state == "open" or sensor_state == "active" else 0,
                "device_class": "motion" if sensor_type == "motion" else "opening",
                "device_class_timestamp": event.Timestamp.isoformat(),
                "signal_strength": sensor_signal * -1,
                "battery_level": sensor_battery
            }

            LOG.debug(event_payload)

            event_topic = WYZESENSE2MQTT_TOPIC_ROOT+"{0}".format(event.MAC)
            client.publish(event_topic, payload = json.dumps(event_payload), qos = MQTT_QOS, retain = MQTT_RETAIN)

            if PERFORM_HASS_DISCOVERY == True:
                send_discovery_topics(event.MAC, sensor_type)

        except TimeoutError as err:
            LOG.debug(err)
        except socket.timeout as err:            
            LOG.debug(err)
        except: # catch *all* exceptions
            e = sys.exc_info()[0]
            LOG.debug("Error: {0}".format(e))

@retry(wait_exponential_multiplier=1000, wait_exponential_max=10000)
def beginConn():
    LOG.debug("In beginConn")
    return Open(USB_DEVICE, on_event)

# Initialize Logging
init_logging()

#Connect to USB
ws = beginConn()

# Configure MQTT Client
client = mqtt.Client(client_id = MQTT_CLIENT_ID, clean_session = MQTT_CLEAN_SESSION)
client.username_pw_set(username = MQTT_USERNAME, password = MQTT_PASSWORD)
client.reconnect_delay_set(min_delay=1, max_delay=120)
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message

# Connect to MQTT and maintain connection
client.connect(MQTT_HOST, port = MQTT_PORT, keepalive = MQTT_KEEPALIVE)
client.loop_forever(retry_first_connection = True)
