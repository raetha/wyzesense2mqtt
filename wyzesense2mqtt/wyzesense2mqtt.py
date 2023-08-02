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

# Used for alternate MQTT connection method
# import signal
# import time

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
    LOGGER.debug("Initializing configuration...")

    # load base config - allows for auto addition of new settings
    if (os.path.isfile(SAMPLES_PATH + MAIN_CONFIG_FILE)):
        CONFIG = read_yaml_file(SAMPLES_PATH + MAIN_CONFIG_FILE)

    # load user config over base
    if (os.path.isfile(CONFIG_PATH + MAIN_CONFIG_FILE)):
        user_config = read_yaml_file(CONFIG_PATH + MAIN_CONFIG_FILE)
        CONFIG.update(user_config)

    # fail on no config
    if (CONFIG is None):
        LOGGER.error(f"Failed to load configuration, please configure.")
        exit(1)

    # write updated config file if needed
    if (CONFIG != user_config):
        LOGGER.info("Writing updated config file")
        write_yaml_file(CONFIG_PATH + MAIN_CONFIG_FILE, CONFIG)


# Initialize MQTT client connection
def init_mqtt_client():
    global MQTT_CLIENT, CONFIG, LOGGER
    # Used for alternate MQTT connection method
    # mqtt.Client.connected_flag = False

    # Configure MQTT Client
    MQTT_CLIENT = mqtt.Client(client_id=CONFIG['mqtt_client_id'], clean_session=CONFIG['mqtt_clean_session'])
    MQTT_CLIENT.username_pw_set(username=CONFIG['mqtt_username'], password=CONFIG['mqtt_password'])
    MQTT_CLIENT.reconnect_delay_set(min_delay=1, max_delay=120)
    MQTT_CLIENT.on_connect = on_connect
    MQTT_CLIENT.on_disconnect = on_disconnect
    MQTT_CLIENT.on_message = on_message
    MQTT_CLIENT.enable_logger(LOGGER)

    # Connect to MQTT
    LOGGER.info(f"Connecting to MQTT host {CONFIG['mqtt_host']}")
    MQTT_CLIENT.connect_async(CONFIG['mqtt_host'], port=CONFIG['mqtt_port'], keepalive=CONFIG['mqtt_keepalive'])

    # Used for alternate MQTT connection method
    # MQTT_CLIENT.loop_start()
    # while (not MQTT_CLIENT.connected_flag):
    #     time.sleep(1)


# Retry forever on IO Error
def retry_if_io_error(exception):
    return isinstance(exception, IOError)


# Initialize USB dongle
@retry(wait_exponential_multiplier=1000, wait_exponential_max=30000, retry_on_exception=retry_if_io_error)
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
        LOGGER.debug(f"Dongle {CONFIG['usb_dongle']}: ["
                     f" MAC: {WYZESENSE_DONGLE.MAC},"
                     f" VER: {WYZESENSE_DONGLE.Version},"
                     f" ENR: {WYZESENSE_DONGLE.ENR}]")
    except IOError as error:
        LOGGER.warning(f"No device found on path {CONFIG['usb_dongle']}: {str(error)}")


# Initialize sensor configuration
def init_sensors():
    # Initialize sensor dictionary
    global SENSORS
    SENSORS = dict()

    # Load config file
    LOGGER.debug("Reading sensors configuration...")
    if (os.path.isfile(CONFIG_PATH + SENSORS_CONFIG_FILE)):
        SENSORS = read_yaml_file(CONFIG_PATH + SENSORS_CONFIG_FILE)
        sensors_config_file_found = True
    else:
        LOGGER.info("No sensors config file found.")
        sensors_config_file_found = False

    # Add invert_state value if missing
    for sensor_mac in SENSORS:
        if (SENSORS[sensor_mac].get('invert_state') is None):
            SENSORS[sensor_mac]['invert_state'] = False

    # Check config against linked sensors
    try:
        result = WYZESENSE_DONGLE.List()
        LOGGER.debug(f"Linked sensors: {result}")
        if (result):
            for sensor_mac in result:
                if (valid_sensor_mac(sensor_mac)):
                    if (SENSORS.get(sensor_mac) is None):
                        add_sensor_to_config(sensor_mac, None, None)
        else:
            LOGGER.warning(f"Sensor list failed with result: {result}")
    except TimeoutError:
        pass

    # Save sensors file if didn't exist
    if (not sensors_config_file_found):
        LOGGER.info("Writing Sensors Config File")
        write_yaml_file(CONFIG_PATH + SENSORS_CONFIG_FILE, SENSORS)

    # Send discovery topics
    if(CONFIG['hass_discovery']):
        for sensor_mac in SENSORS:
            if (valid_sensor_mac(sensor_mac)):
                send_discovery_topics(sensor_mac)


# Validate sensor MAC
def valid_sensor_mac(sensor_mac):
    #LOGGER.debug(f"Validating MAC: {sensor_mac}")
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
    LOGGER.info(f"Adding sensor to config: {sensor_mac}")
    SENSORS[sensor_mac] = dict()
    SENSORS[sensor_mac]['name'] = f"Wyze Sense {sensor_mac}"
    SENSORS[sensor_mac]['class'] = (
        "motion" if (sensor_type == "motion")
        else "opening"
    )
    SENSORS[sensor_mac]['invert_state'] = False
    if (sensor_version is not None):
        SENSORS[sensor_mac]['sw_version'] = sensor_version

    write_yaml_file(CONFIG_PATH + SENSORS_CONFIG_FILE, SENSORS)


# Delete sensor from config
def delete_sensor_from_config(sensor_mac):
    global SENSORS
    LOGGER.info(f"Deleting sensor from config: {sensor_mac}")
    try:
        del SENSORS[sensor_mac]
        write_yaml_file(CONFIG_PATH + SENSORS_CONFIG_FILE, SENSORS)
    except KeyError:
        LOGGER.debug(f"{sensor_mac} not found in SENSORS")


# Publish MQTT topic
def mqtt_publish(mqtt_topic, mqtt_payload):
    global MQTT_CLIENT, CONFIG
    mqtt_message_info = MQTT_CLIENT.publish(
        mqtt_topic,
        payload=json.dumps(mqtt_payload),
        qos=CONFIG['mqtt_qos'],
        retain=CONFIG['mqtt_retain']
    )
    if (mqtt_message_info.rc != mqtt.MQTT_ERR_SUCCESS):
        LOGGER.warning(f"MQTT publish error: {mqtt.error_string(mqtt_message_info.rc)}")


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
        entity_payload['stat_t'] = f"{CONFIG['self_topic_root']}/{sensor_mac}"
        entity_payload['dev'] = device_payload
        sensor_type = ("binary_sensor" if (entity == "state") else "sensor")

        entity_topic = f"{CONFIG['hass_topic_root']}/{sensor_type}/wyzesense_{sensor_mac}/{entity}/config"
        mqtt_publish(entity_topic, entity_payload)
        LOGGER.debug(f"  {entity_topic}")
        LOGGER.debug(f"  {json.dumps(entity_payload)}")


# Clear any retained topics in MQTT
def clear_topics(sensor_mac):
    global CONFIG
    LOGGER.info("Clearing sensor topics")
    state_topic = f"{CONFIG['self_topic_root']}/{sensor_mac}"
    mqtt_publish(state_topic, None)

    # clear discovery topics if configured
    if(CONFIG['hass_discovery']):
        entity_types = ['state', 'signal_strength', 'battery']
        for entity_type in entity_types:
            sensor_type = (
                "binary_sensor" if (entity_type == "state")
                else "sensor"
            )
            entity_topic = f"{CONFIG['hass_topic_root']}/{sensor_type}/wyzesense_{sensor_mac}/{entity_type}/config"
            mqtt_publish(entity_topic, None)


def on_connect(MQTT_CLIENT, userdata, flags, rc):
    global CONFIG
    if rc == mqtt.MQTT_ERR_SUCCESS:
        MQTT_CLIENT.subscribe(
            [(SCAN_TOPIC, CONFIG['mqtt_qos']),
             (REMOVE_TOPIC, CONFIG['mqtt_qos']),
             (RELOAD_TOPIC, CONFIG['mqtt_qos'])]
        )
        MQTT_CLIENT.message_callback_add(SCAN_TOPIC, on_message_scan)
        MQTT_CLIENT.message_callback_add(REMOVE_TOPIC, on_message_remove)
        MQTT_CLIENT.message_callback_add(RELOAD_TOPIC, on_message_reload)
        # Used for alternate MQTT connection method
        # MQTT_CLIENT.connected_flag = True
        LOGGER.info(f"Connected to MQTT: {mqtt.error_string(rc)}")
    else:
        LOGGER.warning(f"Connection to MQTT failed: {mqtt.error_string(rc)}")


def on_disconnect(MQTT_CLIENT, userdata, rc):
    MQTT_CLIENT.message_callback_remove(SCAN_TOPIC)
    MQTT_CLIENT.message_callback_remove(REMOVE_TOPIC)
    MQTT_CLIENT.message_callback_remove(RELOAD_TOPIC)
    # Used for alternate MQTT connection method
    # MQTT_CLIENT.connected_flag = False
    LOGGER.info(f"Disconnected from MQTT: {mqtt.error_string(rc)}")


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
                    if(CONFIG['hass_discovery']):
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
            delete_sensor_from_config(sensor_mac)
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

    # Simplify mapping of device classes.
    DEVICE_CLASSES = {
        'leak': 'moisture',
        'motion': 'motion',
        'switch': 'opening',
    }

    # List of states that correlate to ON.
    STATES_ON = ['active', 'open', 'wet']

    if (valid_sensor_mac(event.MAC)):
        if (event.Type == "alarm") or (event.Type == "status"):
            LOGGER.info(f"State event data: {event}")
            (sensor_type, sensor_state, sensor_battery, sensor_signal) = event.Data

            # Add sensor if it doesn't already exist
            if (event.MAC not in SENSORS):
                add_sensor_to_config(event.MAC, sensor_type, None)
                if(CONFIG['hass_discovery']):
                    send_discovery_topics(event.MAC)

            # Build event payload
            event_payload = {
                'event': event.Type,
                'available': True,
                'mac': event.MAC,
                'device_class': DEVICE_CLASSES.get(sensor_type),
                'last_seen': event.Timestamp.timestamp(),
                'last_seen_iso': event.Timestamp.isoformat(),
                'signal_strength': sensor_signal * -1,
                'battery': sensor_battery
            }

            if (CONFIG['publish_sensor_name']):
                event_payload['name'] = SENSORS[event.MAC]['name']

            # Set state depending on state string and `invert_state` setting.
            #     State ON ^ NOT Inverted = True
            #     State OFF ^ NOT Inverted = False
            #     State ON ^ Inverted = False
            #     State OFF ^ Inverted = True
            event_payload['state'] = int((sensor_state in STATES_ON) ^ (SENSORS[event.MAC].get('invert_state')))

            LOGGER.debug(event_payload)

            state_topic = f"{CONFIG['self_topic_root']}/{event.MAC}"
            mqtt_publish(state_topic, event_payload)
        else:
            LOGGER.debug(f"Non-state event data: {event}")

    else:
        LOGGER.warning("!Invalid MAC detected!")
        LOGGER.warning(f"Event data: {event}")


if __name__ == "__main__":
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

    # Loop forever until keyboard interrupt or SIGINT
    try:
        while True:
            MQTT_CLIENT.loop_forever(retry_first_connection=False)

            # Used for alternate MQTT connection method
            # signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        # Used with alternate MQTT connection method
        # MQTT_CLIENT.loop_stop()

        MQTT_CLIENT.disconnect()
        WYZESENSE_DONGLE.Stop()
