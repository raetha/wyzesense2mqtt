'''
WyzeSense to MQTT Gateway
'''
import json
import logging
import logging.config
import logging.handlers

import paho.mqtt.client as mqtt
import paho.mqtt.subscribe as subscribe

import os

from retrying import retry

import shutil
import subprocess
import yaml
import time

import paho.mqtt.client as mqtt
import wyzesense


# Configuration File Locations
CONFIG_PATH = "config"
SAMPLES_PATH = "samples"
MAIN_CONFIG_FILE = "config.yaml"
LOGGING_CONFIG_FILE = "logging.yaml"
SENSORS_CONFIG_FILE = "sensors.yaml"
SENSORS_STATE_FILE = "state.yaml"


# Simplify mapping of device classes.
# { **dict.fromkeys(['list', 'of', 'possible', 'identifiers'], 'device_class') }
DEVICE_CLASSES = {
    **dict.fromkeys([0x01, 0x0E, 'switch', 'switchv2'], 'opening'),
    **dict.fromkeys([0x02, 0x0F, 'motion', 'motionv2'], 'motion'),
    **dict.fromkeys([0x03, 'leak'], 'moisture'),
    **dict.fromkeys([0x05, 'keypad'], 'alarm_control_panel'),
}


# List of states that correlate to ON.
STATES_ON = ['active', 'open', 'wet']

# Oldest state data that is considered fresh, older state data is stale and ignored
# 1 hour, converted to seconds
STALE_STATE = 1*60*60

# Keep persistant data about the sensors that isn't configurable in a seperate state variable
# Read/write this file to try and maintain a consistent state
SENSORS_STATE = {}


# V1 sensors send state every 4 hours, V2 sensors send every 2 hours
# For timeout of availability, use 2 times the report period to allow
# for one missed message. If 2 are missed, then the sensor probably is
# offline.
DEFAULT_V1_TIMEOUT_HOURS = 8
DEFAULT_V2_TIMEOUT_HOURS = 4


# List of sw versions for V1 and V2 sensors, to determine which timeout to use by default
V1_SW=[19]
V2_SW=[23]

INITIALIZED = False

# Read data from YAML file
def read_yaml_file(filename):
    try:
        with open(filename) as yaml_file:
            data = yaml.safe_load(yaml_file)
            return data
    except IOError as error:
        if LOGGER is None:
            print(f"File error: {str(error)}")
        else:
            LOGGER.error(f"File error: {str(error)}")


# Write data to YAML file
def write_yaml_file(filename, data):
    try:
        with open(filename, 'w') as yaml_file:
            yaml_file.write(yaml.safe_dump(data))
    except IOError as error:
        if LOGGER is None:
            print(f"File error: {str(error)}")
        else:
            LOGGER.error(f"File error: {str(error)}")


# Initialize logging
def init_logging():
    global LOGGER
    if not os.path.isfile(os.path.join(CONFIG_PATH, LOGGING_CONFIG_FILE)):
        print("Copying default logging config file...")
        try:
            shutil.copy2(os.path.join(SAMPLES_PATH, LOGGING_CONFIG_FILE), CONFIG_PATH)
        except IOError as error:
            print(f"Unable to copy default logging config file. {str(error)}")
    logging_config = read_yaml_file(os.path.join(CONFIG_PATH, LOGGING_CONFIG_FILE))

    log_path = os.path.dirname(logging_config['handlers']['file']['filename'])
    try:
        if not os.path.exists(log_path):
            os.makedirs(log_path)
    except IOError:
        print("Unable to create log folder")
    logging.config.dictConfig(logging_config)
    LOGGER = logging.getLogger("wyzesense2mqtt")
    LOGGER.info("Logging initialized...")


# Initialize configuration
def init_config():
    global CONFIG

    LOGGER.info("Initializing configuration...")

    # Initialize CONFIG dictionary with default values
    # Allows for addition of new settings and ensures that missing values will have a default at runtime
    CONFIG = {
        'mqtt_host': None,
        'mqtt_port': 1883,
        'mqtt_username': None,
        'mqtt_password': None,
        'mqtt_client_id': 'wyzesense2mqtt',
        'mqtt_clean_session': False,
        'mqtt_keepalive': 60,
        'mqtt_qos': 0,
        'mqtt_retain': True,
        'self_topic_root': 'wyzesense2mqtt',
        'hass_topic_root': 'homeassistant',
        'hass_discovery': True,
        'publish_sensor_name': True,
        'usb_dongle': 'auto'
    }

    # load config file over default values
    config_from_file = None
    if (os.path.isfile(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE))):
        config_from_file = read_yaml_file(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE))
        CONFIG.update(config_from_file)

    # load ENV supplied config over default values and config file values
    for key,value in os.environ.items():
        key = str(key).lower()
        if key in CONFIG:
            if value.isnumeric():
                value = int(value)
            elif value.lower() == 'true':
                value = True
            elif value.lower() == 'false':
                value = False
            elif value.lower() == 'none':
                value = None
            CONFIG.update({key: value})

    # fail on no config
    if CONFIG is None:
        LOGGER.error(f"Failed to load configuration, please configure.")
        exit(1)

    # write updated config file if needed
    if (config_from_file is None or CONFIG != config_from_file):
        LOGGER.info("Writing updated config file")
        write_yaml_file(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE), CONFIG)


# Initialize MQTT client connection
def init_mqtt_client():
    global MQTT_CLIENT, CONFIG, LOGGER
    # Used for alternate MQTT connection method
    mqtt.Client.connected_flag = False

    # Configure MQTT Client
    if not hasattr(mqtt, "CallbackAPIVersion"):
        # paho-mqtt 1.x
        MQTT_CLIENT = mqtt.Client(client_id=CONFIG['mqtt_client_id'], clean_session=CONFIG['mqtt_clean_session'])
    else:
        # paho-mqtt 2.x
        MQTT_CLIENT = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,client_id=CONFIG['mqtt_client_id'], clean_session=CONFIG['mqtt_clean_session'])

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
    MQTT_CLIENT.loop_start()
    while (not MQTT_CLIENT.connected_flag):
        time.sleep(1)

    # Make sure the service stays marked as offline until everything is initialized
    mqtt_publish(f"{CONFIG['self_topic_root']}/status", "offline", is_json=False)

# Retry forever on IO Error
def retry_if_io_error(exception):
    return isinstance(exception, IOError)


# Initialize USB dongle
@retry(
    wait_exponential_multiplier=1000,
    wait_exponential_max=30000,
    retry_on_exception=retry_if_io_error,
)
def init_wyzesense_dongle():
    global WYZESENSE_DONGLE, CONFIG, LOGGER
    if (CONFIG['usb_dongle'].lower() == "auto"):
        device_list = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode("utf-8").lower()
        for line in device_list.split("\n"):
            if ("e024" in line) and ("1a86" in line):
                for device_name in line.split(" "):
                    if "hidraw" in device_name:
                        CONFIG['usb_dongle'] = f"/dev/{device_name}"
                        break

    LOGGER.info(f"Connecting to dongle {CONFIG['usb_dongle']}")
    try:
        WYZESENSE_DONGLE = wyzesense.Open(CONFIG['usb_dongle'], on_event, LOGGER)
        LOGGER.info(f"Dongle {CONFIG['usb_dongle']}: ["
                    f" MAC: {WYZESENSE_DONGLE.MAC},"
                    f" VER: {WYZESENSE_DONGLE.Version},"
                    f" ENR: {WYZESENSE_DONGLE.ENR}]")
    except IOError as error:
        LOGGER.error(f"No device found on path {CONFIG['usb_dongle']}: {str(error)}")


# Initialize sensor configuration
def init_sensors(wait=True):
    # Initialize sensor dictionary
    global SENSORS, SENSORS_STATE
    SENSORS = {}

    # Load config file
    if (os.path.isfile(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE))):
        LOGGER.info("Reading sensors configuration...")
        SENSORS = read_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE))
        sensors_config_file_found = True
    else:
        LOGGER.warning("No sensors config file found.")
        sensors_config_file_found = False

    # Add invert_state value if missing
    if SENSORS:
        for sensor_mac in SENSORS:
            if SENSORS[sensor_mac].get('invert_state') is None:
                SENSORS[sensor_mac]['invert_state'] = False

    # Load previous known states
    if (os.path.isfile(os.path.join(CONFIG_PATH, SENSORS_STATE_FILE))):
        LOGGER.info("Reading sensors last known state...")
        SENSORS_STATE = read_yaml_file(os.path.join(CONFIG_PATH, SENSORS_STATE_FILE))
        if (SENSORS_STATE.get('modified') is not None):
            if ((time.time() - SENSORS_STATE['modified']) > STALE_STATE):
                LOGGER.warning("Ignoring stale state data")
                SENSORS_STATE = {}
            else:
                # Remove this field so we don't get a bogus warning below
                del SENSORS_STATE['modified']

    # Check config against linked sensors
    checked_linked = False
    try:
        LOGGER.info("Checking sensors against dongle list...")
        result = WYZESENSE_DONGLE.List()
        if (result):
            checked_linked = True

            for sensor_mac in result:
                if valid_sensor_mac(sensor_mac):
                    if SENSORS.get(sensor_mac) is None:
                        add_sensor_to_config(sensor_mac, None, None)
                        LOGGER.warning(f"Linked sensor with mac {sensor_mac} automatically added to sensors configuration")
                        LOGGER.warning(f"Please update sensor configuration file {os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)} restart the service/reload the sensors")

                    # If not a configured sensor, then adding it will also add it to the state
                    # So only check if in the state if it is a configured sensor
                    elif (SENSORS_STATE.get(sensor_mac) is None):
                        # Only track state for linked sensors
                        # If it wasn't configured, it'd get added above, including in state
                        # Intialize last seen time to now and start online
                        SENSORS_STATE[sensor_mac] = {
                            'last_seen': time.time(),
                            'online': True
                        }

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
            if (SENSORS_STATE.get(sensor_mac) is None):
                # Intialize last seen time to now and start online
                SENSORS_STATE[sensor_mac] = {
                    'last_seen': time.time(),
                    'online': True
                }


    # Save sensors file if didn't exist
    if not sensors_config_file_found:
        LOGGER.info("Writing Sensors Config File")
        write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)

    # Send discovery topics
    if(CONFIG['hass_discovery']):
        for sensor_mac in SENSORS_STATE:
            if (valid_sensor_mac(sensor_mac)):
                send_discovery_topics(sensor_mac, wait=wait)


# Validate sensor MAC
def valid_sensor_mac(sensor_mac):
    invalid_mac_list = [
        "00000000",
        "\0\0\0\0\0\0\0\0",
        "\x00\x00\x00\x00\x00\x00\x00\x00",
    ]

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
def add_sensor_to_config(sensor_mac, sensor_type, sensor_version):
    global SENSORS, SENSORS_STATE
    LOGGER.info(f"Adding sensor to config: {sensor_mac}")
    SENSORS[sensor_mac] = {
        'name': f"Wyze Sense {sensor_mac}",
        'invert_state': False
    }

    SENSORS[sensor_mac]['class'] = "opening" if sensor_type is None else DEVICE_CLASSES.get(sensor_type)

    if sensor_version is not None:
        'class': DEVICE_CLASSES.get(sensor_type),
        'sw_version': sensor_version,
    }

    if DEVICE_CLASSES.get(sensor_type) == "alarm_control_panel":
        SENSORS[sensor_mac].update(
            {'pin': '0000', 'arm_required': True, 'disarm_required': True}
        )

    # Intialize last seen time to now and start online
    SENSORS_STATE[sensor_mac] = {
        'last_seen': time.time(),
        'online': True
    }

    write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)


# Delete sensor from config
def delete_sensor_from_config(sensor_mac):
    global SENSORS, SENSORS_STATE
    LOGGER.info(f"Deleting sensor from config: {sensor_mac}")
    try:
        del SENSORS[sensor_mac]
        write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)
        del SENSORS_STATE[sensor_mac]
    except KeyError:
        LOGGER.error(f"{sensor_mac} not found in SENSORS")


# Publish MQTT topic
def mqtt_publish(mqtt_topic, mqtt_payload, is_json=True, wait=True):
    global MQTT_CLIENT, CONFIG
    payload = json.dumps(mqtt_payload) if is_json else mqtt_payload
    LOGGER.debug(f"Publishing, {mqtt_topic=}, {payload=}")
    mqtt_message_info = MQTT_CLIENT.publish(
        mqtt_topic,
        payload=payload,
        qos=CONFIG['mqtt_qos'],
        retain=CONFIG['mqtt_retain'],
    )

    if (mqtt_message_info.rc == mqtt.MQTT_ERR_SUCCESS):
        if (wait):
            mqtt_message_info.wait_for_publish(2)
        return

    LOGGER.warning(f"MQTT publish error: {mqtt.error_string(mqtt_message_info.rc)}")


def mqtt_simple_subscribe(mqtt_topic):
    global MQTT_CLIENT, CONFIG
    msg = subscribe.simple(
        mqtt_topic,
        qos=CONFIG['mqtt_qos'],
        retained=True,
        hostname=CONFIG['mqtt_host'],
        port=CONFIG['mqtt_port'],
        client_id=CONFIG['mqtt_client_id'],
        auth={'username': CONFIG['mqtt_username'], 'password': CONFIG['mqtt_password']}
        if CONFIG['mqtt_username']
        else None,
        keepalive=CONFIG['mqtt_keepalive'],
    )

    return json.loads(msg.payload)


# Send discovery topics
def send_discovery_topics(sensor_mac, wait=True):
    global SENSORS, CONFIG, SENSORS_STATE

    LOGGER.info(f"Publishing discovery topics for {sensor_mac}")

    sensor_name = SENSORS[sensor_mac]['name']
    sensor_class = SENSORS[sensor_mac]['class']
    if SENSORS[sensor_mac].get('sw_version') is not None:
        sensor_version = SENSORS[sensor_mac]['sw_version']
    else:
        sensor_version = ""

    model = "Sense {}"
    if sensor_class == "motion":
        model = model.format("Motion Sensor")
    elif sensor_class == "opening":
        model = model.format("Contact Sensor")
    elif sensor_class == "keypad":
        model = model.format("Keypad")
    else:
        model = model.format("Device")

    mac_topic = f"{CONFIG['self_topic_root']}/{sensor_mac}"

    entity_payloads = {
        'state': {
            'name': None,
            'device_class': sensor_class,
            'payload_on': "1",
            'payload_off': "0",
            'json_attributes_topic': mac_topic,
            'device' : {
               'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
               'manufacturer': "Wyze",
               'model': f"Sense {model}",
               'name': sensor_name,
               'sw_version': sensor_version,
               'via_device': "wyzesense2mqtt"
            }
        },
        'signal_strength': {
            'device_class': "signal_strength",
            'state_class': "measurement",
            'unit_of_measurement': "%",
            'entity_category': "diagnostic",
            'device' : {
               'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
               'name': f"{sensor_name} Signal Strength",
            }
        },
        'battery': {
            'device_class': "battery",
            'state_class': "measurement",
            'unit_of_measurement': "%",
            'entity_category': "diagnostic",
            'device' : {
               'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
               'name': f"{sensor_name} Battery",
            }
        }
    }

    availability_topics = [
        { 'topic': f"{CONFIG['self_topic_root']}/{sensor_mac}/status" },
        { 'topic': f"{CONFIG['self_topic_root']}/status" }
    ]

    if sensor_class == "alarm_control_panel":
        entity_payloads.update(
            {
                'mode': {
                    'name': sensor_name,
                    'pl_arm_away': "armed_away",
                    'pl_arm_home': "armed_home",
                    'pl_disarm': "disarmed",
                    'code': SENSORS[sensor_mac]['pin'],
                    'code_arm_required': SENSORS[sensor_mac]['arm_required'],
                    'code_disarm_required': SENSORS[sensor_mac]['disarm_required'],
                    'stat_t': f"{CONFIG['self_topic_root']}/{sensor_mac}/mode",
                    'val_tpl': f"{{{{ value_json.state }}}}",
                    'cmd_t': f"{CONFIG['self_topic_root']}/{sensor_mac}/set",
                    'cmd_tpl': '{ "mode": "{{ action }}", "pin": "{{ code }}" }',
                },
                'motion': {
                    'name': f"{sensor_name} Motion",
                    'dev_cla': 'motion',
                    'pl_on': "1",
                    'pl_off': "0",
                    'val_tpl': f"{{{{ value_json.state }}}}",
                    'stat_t': f"{CONFIG['self_topic_root']}/{sensor_mac}/motion",
                },
            }
        )

        sensor_type = ""
        if sensor_class != "alarm_control_panel":
            sensor_type = "binary_sensor" if (entity == "state") else "sensor"
        else:
            if entity == "mode":
                sensor_type = "alarm_control_panel"
                entity = "state"
            elif entity == "motion":
                sensor_type = "binary_sensor"
                entity = "state"
            else:
                sensor_type = "sensor"

    for entity, entity_payload in entity_payloads.items():
        entity_payload['value_template'] = f"{{{{ value_json.{entity} }}}}"
        entity_payload['unique_id'] = f"wyzesense_{sensor_mac}_{entity}"
        entity_payload['state_topic'] = mac_topic
        entity_payload['availability'] = availability_topics
        entity_payload['availability_mode'] = "all"
        entity_payload['platform'] = "mqtt"

        sensor_type = ""
        if sensor_class != "alarm_control_panel":
            sensor_type = "binary_sensor" if (entity == "state") else "sensor"
        else:
            if entity == "mode":
                sensor_type = "alarm_control_panel"
                entity = "state"
            elif entity == "motion":
                sensor_type = "binary_sensor"
                entity = "state"
            else:
                sensor_type = "sensor"

        entity_topic = f"{CONFIG['hass_topic_root']}/{sensor_type}/wyzesense_{sensor_mac}/{entity}/config"
        mqtt_publish(entity_topic, entity_payload, wait=wait)

        LOGGER.info(f"  {entity_topic}")
        LOGGER.info(f"  {json.dumps(entity_payload)}")
    mqtt_publish(f"{CONFIG['self_topic_root']}/{sensor_mac}/status", "online" if SENSORS_STATE[sensor_mac]['online'] else "offline", is_json=False, wait=wait)

# Clear any retained topics in MQTT
def clear_topics(sensor_mac):
    global CONFIG
    LOGGER.info("Clearing sensor topics")
    state_topic = f"{CONFIG['self_topic_root']}/{sensor_mac}"
    mqtt_publish(state_topic, None, wait=wait)
    mqtt_publish(f"{state_topic}/status}", None, wait=wait)

    if SENSORS[sensor_mac]['class'] == 'alarm_control_panel':
        for i in ['mode', 'motion', 'pin', 'set']:
            mqtt_publish(f"{state_topic}/{i}", None, wait=wait)

    # clear discovery topics if configured
    if CONFIG['hass_discovery']:
        entity_types = {
            'sensor': ['battery', 'signal_strength'],
            'binary_sensor': ['state'],
            'alarm_control_panel': ['state'],
        }

        for entity_type in entity_types:
            for entity in entity_types[entity_type]:
                entity_topic = f"{CONFIG['hass_topic_root']}/{entity_type}/wyzesense_{sensor_mac}"
                mqtt_publish(f"{entity_topic}/{entity_type}/config", None, wait=wait)
                mqtt_publish(f"{entity_topic}/{entity_type}", None, wait=wait)
                mqtt_publish(entity_topic, None, wait=wait)


def on_connect(MQTT_CLIENT, userdata, flags, rc):
    global CONFIG
    if rc == mqtt.MQTT_ERR_SUCCESS:
        MQTT_CLIENT.subscribe(
            [
                (SCAN_TOPIC, CONFIG['mqtt_qos']),
                (REMOVE_TOPIC, CONFIG['mqtt_qos']),
                (RELOAD_TOPIC, CONFIG['mqtt_qos']),
                (SET_TOPIC, CONFIG['mqtt_qos']),
            ]
        )
        MQTT_CLIENT.message_callback_add(SCAN_TOPIC, on_message_scan)
        MQTT_CLIENT.message_callback_add(REMOVE_TOPIC, on_message_remove)
        MQTT_CLIENT.message_callback_add(RELOAD_TOPIC, on_message_reload)
        MQTT_CLIENT.message_callback_add(SET_TOPIC, on_message_set)
        MQTT_CLIENT.connected_flag = True

        LOGGER.info(f"Connected to MQTT: {mqtt.error_string(rc)}")
    else:
        LOGGER.warning(f"Connection to MQTT failed: {mqtt.error_string(rc)}")


def on_disconnect(MQTT_CLIENT, userdata, rc):
    MQTT_CLIENT.message_callback_remove(SCAN_TOPIC)
    MQTT_CLIENT.message_callback_remove(REMOVE_TOPIC)
    MQTT_CLIENT.message_callback_remove(RELOAD_TOPIC)
    MQTT_CLIENT.message_callback_remove(SET_TOPIC)
    MQTT_CLIENT.connected_flag = False

    LOGGER.info(f"Disconnected from MQTT: {mqtt.error_string(rc)}")


# We don't handle any additional messages from MQTT, just log them
def on_message(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"{msg.topic}: {str(msg.payload)}")


# Process message to scan for new sensors
def on_message_scan(MQTT_CLIENT, userdata, msg):
    global SENSORS, CONFIG
    result = None
    LOGGER.info(f"In on_message_scan: {msg.payload.decode()}")

    # The scan will do a couple additional calls even after the new sensor is found
    # These calls may time out, so catch it early so we can still add the sensor properly
    try:
        result = WYZESENSE_DONGLE.Scan()
    except TimeoutError:
        pass

    if (result):
        LOGGER.info(f"Scan result: {result}")
        sensor_mac, sensor_type, sensor_version = result
        if (valid_sensor_mac(sensor_mac)):
            if (SENSORS.get(sensor_mac)) is None:
                add_sensor_to_config(sensor_mac, sensor_type, sensor_version)
                if(CONFIG['hass_discovery']):
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

    if (valid_sensor_mac(sensor_mac)):
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


# Process message to set keypad state
def on_message_set(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"In on_message_set: {msg.payload.decode()}")
    parts = msg.topic.split('/')
    payload = json.loads(msg.payload)
    if payload and payload['mode'] is not None:
        set_keypad_mode(parts[1], payload)


def set_keypad_mode(keypad_mac, payload):
    mode = payload['mode']
    pin = payload['pin']

    if not validate_pin_entry(keypad_mac, mode, pin):
        return

    mode_payload = {'state': mode}

    mode_topic = f"{CONFIG['self_topic_root']}/{keypad_mac}/mode"
    set_topic = f"{CONFIG['self_topic_root']}/{keypad_mac}/set"

    mqtt_publish(mode_topic, mode_payload)
    mqtt_publish(set_topic, {'mode': None, 'pin': False})


def validate_pin_entry(mac, mode, pin=None):
    pin_topic = f"{CONFIG['self_topic_root']}/{mac}/pin"
    config = SENSORS[mac]

    if pin is None:
        pin = mqtt_simple_subscribe(pin_topic)['state']

    if pin != config['pin']:
        if (mode in ["armed_away", "armed_home"] and config['arm_required']) or (
            mode == "disarmed" and config['disarm_required']
        ):
            return False

    return True


# Process event
def on_event(WYZESENSE_DONGLE, event):
    global SENSORS, SENSORS_STATE

    if not INITIALIZED:
        return

    if event == "ERROR":
        mqtt_publish(f"{CONFIG['self_topic_root']}/status", "offline", is_json=False)
        return

    if valid_sensor_mac(event.MAC):
        # Ensure sensor exists (auto-add on first sight)
        if event.MAC not in SENSORS:
            # If we haven't parsed event.Data yet, we don't know event_type; pass None for now.
            add_sensor_to_config(event.MAC, None, None)
            if CONFIG.get('hass_discovery'):
                send_discovery_topics(event.MAC)
            LOGGER.warning(f"Linked sensor with mac {event.MAC} automatically added to sensors configuration")
            LOGGER.warning(
                f"Please update sensor configuration file {os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)} "
                "and restart the service/reload the sensors"
            )

        # Availability tracking
        # Store last seen time for availability in SENSORS_STATE
        if event.MAC not in SENSORS_STATE:
            SENSORS_STATE[event.MAC] = {}
        SENSORS_STATE[event.MAC]['last_seen'] = event.Timestamp.timestamp()

        # Publish LWT-style status topic
        mqtt_publish(f"{CONFIG['self_topic_root']}/{event.MAC}/status", "online", is_json=False)

        # Flip internal online flag if it was offline
        if not SENSORS_STATE[event.MAC].get('online', True):
            SENSORS_STATE[event.MAC]['online'] = True
            LOGGER.info(f"{event.MAC} is back online!")
        else:
            # Ensure the field is present
            SENSORS_STATE[event.MAC]['online'] = True

        # Base payload present for all valid events
        base_topic = f"{CONFIG['self_topic_root']}/{event.MAC}"
        event_payload = {
            'event': event.Type,
            'available': True,
            'mac': event.MAC,
            'last_seen': event.Timestamp.timestamp(),
            'last_seen_iso': event.Timestamp.isoformat(),
        }
        if CONFIG.get('publish_sensor_name') and event.MAC in SENSORS and 'name' in SENSORS[event.MAC]:
            event_payload['name'] = SENSORS[event.MAC]['name']

        # Handle state-like events (alarm/status/keypad provide Data tuple)
        if event.Type in ["alarm", "status", "keypad"]:
            LOGGER.info(f"State event data: {event}")
            try:
                # Feature branch semantics: (event_type, sensor_state, sensor_battery, sensor_signal)
                (event_type, sensor_state, sensor_battery, sensor_signal) = event.Data
            except Exception as ex:
                LOGGER.exception(f"Unexpected event.Data format for {event}: {ex}")
                return

            # Make sure we saved the specific event_type for this sensor if we only had None before
            if event.MAC in SENSORS and (SENSORS[event.MAC].get('type') is None):
                try:
                    add_sensor_to_config(event.MAC, event_type, None)
                    if CONFIG.get('hass_discovery'):
                        send_discovery_topics(event.MAC)
                except Exception:
                    # Don't hard-fail if config write fails; continue publishing.
                    LOGGER.warning(f"Could not persist sensor type for {event.MAC}")

            # Common signal/battery fields
            # Convert to dBm and a rough % (matching your original mapping)
            signal_dbm = (sensor_signal or 0) * -1
            signal_pct = min(max(2 * (signal_dbm + 115), 1), 100)
            event_payload.update({
                'signal_dbm': signal_dbm,
                'signal_strength': signal_pct,
                'battery': sensor_battery,
            })

            # Special handling for V2 switch battery reporting (HALF value) from original branch
            if event_type == "switchv2":
                try:
                    event_payload['battery'] = min((sensor_battery or 0) * 2, 100)
                except Exception:
                    pass
            else:
                # Cap battery at 100% for all others (original behavior)
                try:
                    event_payload['battery'] = min(sensor_battery or 0, 100)
                except Exception:
                    pass

            # ALARM / STATUS: publish state and device class
            if event.Type in ["alarm", "status"]:
                # Device class (if available)
                if 'DEVICE_CLASSES' in globals():
                    event_payload['device_class'] = DEVICE_CLASSES.get(event_type)

                # Compute boolean state, honoring invert_state (original behavior)
                invert = bool(SENSORS[event.MAC].get('invert_state')) if event.MAC in SENSORS else False
                try:
                    event_payload['state'] = int((sensor_state in STATES_ON) ^ invert)
                except Exception:
                    # If sensor_state is not in expected form, fallback to raw
                    event_payload['state'] = sensor_state

                mqtt_publish(base_topic, event_payload)
                LOGGER.debug(event_payload)

            # KEYPAD: publish state and action-specific topics
            elif event.Type == "keypad":
                # For keypad, `sensor_state` is a string like mode/motion/pin events
                state_payload = {"state": sensor_state}

                event_topic = f"{base_topic}/{event_type}"
                pin_topic = f"{base_topic}/pin"
                set_topic = f"{base_topic}/set"

                # Publish base event payload (availability/metadata)
                mqtt_publish(base_topic, event_payload)

                # For mode changes, validate the PIN entry if required
                if event_type == "mode" and 'validate_pin_entry' in globals():
                    if not validate_pin_entry(event.MAC, sensor_state):
                        return

                if event_type in ["mode", "motion"]:
                    mqtt_publish(event_topic, state_payload)
                    mqtt_publish(pin_topic, {'state': False})
                    # Clear set request if we finished a mode set or motion ended
                    if event_type == "mode" or sensor_state == "inactive":
                        mqtt_publish(set_topic, {'mode': None, 'pin': False})
                elif event_type in ['pinStart', 'pinConfirm']:
                    mqtt_publish(pin_topic, state_payload)

                # Echo final state in the event payload for logging/consumers
                event_payload.update(state_payload)
                LOGGER.debug(event_payload)

        else:
            # Events without Data (or other types) — log verbosely
            LOGGER.info(f"{event}")

    else:
        LOGGER.warning("!Invalid MAC detected!")
        LOGGER.warning(f"Event data: {event}")


def Stop():
    # Stop the dongle first, letting this thread finish anything it might be busy doing, like handling an event
    WYZESENSE_DONGLE.Stop()

    mqtt_publish(f"{CONFIG['self_topic_root']}/status", "offline", is_json=False)

    # All event handling should now be done, close the mqtt connection
    MQTT_CLIENT.loop_stop()
    MQTT_CLIENT.disconnect()

    # Save off the last known state
    LOGGER.info("Writing Sensors State File")
    SENSORS_STATE['modified'] = time.time()
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
    SET_TOPIC = f"{CONFIG['self_topic_root']}/+/set"

    # Initialize MQTT client connection
    init_mqtt_client()

    # Initialize USB dongle
    init_wyzesense_dongle()

    # Initialize sensor configuration
    init_sensors()

    # All initialized now, so set the flag to allow message event to be processed
    INITIALIZED = True

    # And mark the service as online
    mqtt_publish(f"{CONFIG['self_topic_root']}/status", "online", is_json=False)

    dongle_offline = False

    # Loop forever until keyboard interrupt or SIGINT
    try:
        loop_counter = 0
        while True:
            time.sleep(5)
            # Check if there is any exceptions in the dongle thread
            WYZESENSE_DONGLE.CheckError()

            # Skip everything while dongle is offline, service needs to be restarted
            if dongle_offline:
                continue

            loop_counter += 1

            if not MQTT_CLIENT.connected_flag:
                LOGGER.warning("Reconnecting MQTT...")
                MQTT_CLIENT.reconnect()

            if MQTT_CLIENT.connected_flag:
                mqtt_publish(f"{CONFIG['self_topic_root']}/status", "online", is_json=False)

            # Every minute, try to get the dongle mac address. Hopefully this will tell us if we are having trouble
            # communicating with the dongle. If so, set the service offline
            if loop_counter > 12:
                loop_counter = 0
                try:
                    WYZESENSE_DONGLE._GetMac()
                except TimeoutError:
                    LOGGER.error("Failed to communicate with dongle")
                    dongle_offline = True
                    mqtt_publish(f"{CONFIG['self_topic_root']}/status", "offline", is_json=False)

            # Check for availability of the devices
            now = time.time()
            for mac in SENSORS_STATE:
                if SENSORS_STATE[mac]['online']:
                    LOGGER.debug(f"Checking availability of {mac}")
                    # If there is a timeout configured, use that. Must be in seconds.
                    # If no timeout configured, check if it's a V2 device (quicker reporting period)
                    # Otherwise, use the longer V1 timeout period
                    if (SENSORS[mac].get('timeout') is not None):
                        timeout = SENSORS[mac]['timeout']
                    elif (SENSORS[mac].get('sw_version') is not None and SENSORS[mac]['sw_version'] in V2_SW):
                        timeout = DEFAULT_V2_TIMEOUT_HOURS*60*60
                    else:
                        timeout = DEFAULT_V1_TIMEOUT_HOURS*60*60

                    if ((now - SENSORS_STATE[mac]['last_seen']) > timeout):
                        mqtt_publish(f"{CONFIG['self_topic_root']}/{mac}/status", "offline", is_json=False)
                        LOGGER.warning(f"{mac} has gone offline!")
                        SENSORS_STATE[mac]['online'] = False
    except KeyboardInterrupt:
        LOGGER.warning("User interrupted")
    except Exception as e:
        LOGGER.error("An error occurred", exc_info=True)
    finally:
        Stop()
