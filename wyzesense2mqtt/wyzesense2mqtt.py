'''
WyzeSense to MQTT Gateway
'''
import json
import logging
import logging.config
import logging.handlers
import paho.mqtt.subscribe as subscribe
import os
import shutil
import subprocess
import yaml


import paho.mqtt.client as mqtt
import wyzesense
from retrying import retry


# Configuration File Locations
CONFIG_PATH = "config"
SAMPLES_PATH = "samples"
MAIN_CONFIG_FILE = "config.yaml"
LOGGING_CONFIG_FILE = "logging.yaml"
SENSORS_CONFIG_FILE = "sensors.yaml"

# Simplify mapping of device classes.
# { **dict.fromkeys(['list', 'of', 'possible', 'identifiers'], 'device_class') }
DEVICE_CLASSES = {
    **dict.fromkeys([0x01, 0x0E, 'switch', 'switchv2'], 'opening'),
    **dict.fromkeys([0x02, 0x0F, 'motion', 'motionv2'], 'motion'),
    **dict.fromkeys([0x03, 'leak'], 'moisture'),
    **dict.fromkeys([0x05, 'keypad'], 'alarm_control_panel'),
}

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
    if (not os.path.isfile(os.path.join(CONFIG_PATH, LOGGING_CONFIG_FILE))):
        print("Copying default logging config file...")
        try:
            shutil.copy2(os.path.join(SAMPLES_PATH, LOGGING_CONFIG_FILE), CONFIG_PATH)
        except IOError as error:
            print(f"Unable to copy default logging config file. {str(error)}")
    logging_config = read_yaml_file(os.path.join(CONFIG_PATH, LOGGING_CONFIG_FILE))

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
    if (os.path.isfile(os.path.join(SAMPLES_PATH, MAIN_CONFIG_FILE))):
        CONFIG = read_yaml_file(os.path.join(SAMPLES_PATH, MAIN_CONFIG_FILE))

    # load user config over base
    if (os.path.isfile(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE))):
        user_config = read_yaml_file(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE))
        CONFIG.update(user_config)

    # fail on no config
    if (CONFIG is None):
        LOGGER.error(f"Failed to load configuration, please configure.")
        exit(1)

    # write updated config file if needed
    if (CONFIG != user_config):
        LOGGER.info("Writing updated config file")
        write_yaml_file(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE), CONFIG)


# Initialize MQTT client connection
def init_mqtt_client():
    global MQTT_CLIENT, CONFIG, LOGGER

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
    MQTT_CLIENT.connect_async(
        CONFIG['mqtt_host'],
        port=CONFIG['mqtt_port'],
        keepalive=CONFIG['mqtt_keepalive'],
    )


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
                        CONFIG['usb_dongle'] = f"/dev/{device_name}"
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
    SENSORS = {}

    # Load config file
    LOGGER.debug("Reading sensors configuration...")
    if (os.path.isfile(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE))):
        SENSORS = read_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE))
        sensors_config_file_found = True
    else:
        LOGGER.info("No sensors config file found.")
        sensors_config_file_found = False

    # Add invert_state value if missing
    if SENSORS:
        for sensor_mac in SENSORS:
            if SENSORS[sensor_mac].get('invert_state') is None:
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
        write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)

    # Send discovery topics
    if CONFIG['hass_discovery']:
        if SENSORS:
            for sensor_mac in SENSORS:
                if valid_sensor_mac(sensor_mac):
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
    SENSORS[sensor_mac] = {
        'name': f"Wyze Sense {sensor_mac}",
        'class': DEVICE_CLASSES.get(sensor_type),
        'invert_state': False,
    }

    if DEVICE_CLASSES.get(sensor_type) == "alarm_control_panel":
        SENSORS[sensor_mac].update(
            {'pin': '0000', 'arm_required': True, 'disarm_required': True}
        )

    if sensor_version is not None:
        SENSORS[sensor_mac]['sw_version'] = sensor_version

    write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)


# Delete sensor from config
def delete_sensor_from_config(sensor_mac):
    global SENSORS
    LOGGER.info(f"Deleting sensor from config: {sensor_mac}")
    try:
        del SENSORS[sensor_mac]
        write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)
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
def send_discovery_topics(sensor_mac):
    global SENSORS, CONFIG

    LOGGER.info(f"Publishing discovery topics for {sensor_mac}")

    sensor_name = SENSORS[sensor_mac]['name']
    sensor_class = SENSORS[sensor_mac]['class']
    if (SENSORS[sensor_mac].get('sw_version') is not None):
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

    device_payload = {
        'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
        'manufacturer': "Wyze",
        'model': model,
        'name': sensor_name,
        'sw_version': sensor_version,
    }

    entity_payloads = {
        'signal_strength': {
            'name': f"{sensor_name} Signal Strength",
            'dev_cla': "signal_strength",
            'unit_of_meas': "dBm",
        },
        'battery': {
            'name': f"{sensor_name} Battery",
            'dev_cla': "battery",
            'unit_of_meas': "%",
        },
    }

    if sensor_class != "alarm_control_panel":
        entity_payloads.update(
            {
                'state': {
                    'name': sensor_name,
                    'dev_cla': sensor_class,
                    'pl_on': "1",
                    'pl_off': "0",
                    'json_attr_t': f"{CONFIG['self_topic_root']}/{sensor_mac}",
                },
            }
        )
    else:
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

    for entity, entity_payload in entity_payloads.items():
        entity_payload['val_tpl'] = entity_payload.get(
            'val_tpl', f"{{{{ value_json.{entity} }}}}"
        )
        entity_payload['uniq_id'] = f"wyzesense_{sensor_mac}_{entity}"
        entity_payload['stat_t'] = entity_payload.get(
            'stat_t', f"{CONFIG['self_topic_root']}/{sensor_mac}"
        )
        entity_payload['dev'] = device_payload
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
        mqtt_publish(entity_topic, entity_payload)
        LOGGER.debug(f"  {entity_topic}")
        LOGGER.debug(f"  {json.dumps(entity_payload)}")


# Clear any retained topics in MQTT
def clear_topics(sensor_mac):
    global CONFIG, SENSORS
    LOGGER.info("Clearing sensor topics")
    state_topic = f"{CONFIG['self_topic_root']}/{sensor_mac}"
    mqtt_publish(state_topic, None)
    if SENSORS[sensor_mac]['class'] == 'alarm_control_panel':
        for i in ['/mode', '/motion', '/pin', '/set']:
            mqtt_publish(state_topic + i, None)

    # clear discovery topics if configured
    if CONFIG['hass_discovery']:
        entity_types = {
            'sensor': ['battery', 'signal_strength'],
            'binary_sensor': ['state'],
            'alarm_control_panel': ['state'],
        }

        for entity_type in entity_types:
            for entity in entity_types[entity_type]:
                entity_topic = f"{CONFIG['hass_topic_root']}/{entity_type}/wyzesense_{sensor_mac}/{entity}/config"
                mqtt_publish(entity_topic, None)


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

        LOGGER.info(f"Connected to MQTT: {mqtt.error_string(rc)}")
    else:
        LOGGER.warning(f"Connection to MQTT failed: {mqtt.error_string(rc)}")


def on_disconnect(MQTT_CLIENT, userdata, rc):
    MQTT_CLIENT.message_callback_remove(SCAN_TOPIC)
    MQTT_CLIENT.message_callback_remove(REMOVE_TOPIC)
    MQTT_CLIENT.message_callback_remove(RELOAD_TOPIC)
    MQTT_CLIENT.message_callback_remove(SET_TOPIC)

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
    global SENSORS

    # List of states that correlate to ON.
    STATES_ON = ['active', 'open', 'wet']

    if valid_sensor_mac(event.MAC):
        # Build event payload
        event_payload = {
            'event': event.Type,
            'available': True,
            'mac': event.MAC,
            'last_seen': event.Timestamp.timestamp(),
            'last_seen_iso': event.Timestamp.isoformat(),
        }

        if event.Type in ["alarm", "status", "keypad"]:
            LOGGER.info(f"State event data: {event}")
            (event_type, sensor_state, sensor_battery, sensor_signal) = event.Data
            event_payload.update(
                {'signal_strength': sensor_signal * -1, 'battery': sensor_battery}
            )

            # Add sensor if it doesn't already exist
            if event.MAC not in SENSORS:
                add_sensor_to_config(event.MAC, event_type, None)
                if CONFIG['hass_discovery']:
                    send_discovery_topics(event.MAC)

        if event.Type in ["alarm", "status"]:
            event_payload['device_class'] = DEVICE_CLASSES.get(event_type)
            state_topic = f"{CONFIG['self_topic_root']}/{event.MAC}"

            if CONFIG['publish_sensor_name']:
                event_payload['name'] = SENSORS[event.MAC]['name']

            # Set state depending on state string and `invert_state` setting.
            #     State ON ^ NOT Inverted = True
            #     State OFF ^ NOT Inverted = False
            #     State ON ^ Inverted = False
            #     State OFF ^ Inverted = True
            event_payload['state'] = int(
                (sensor_state in STATES_ON) ^ (SENSORS[event.MAC].get('invert_state'))
            )

            mqtt_publish(state_topic, event_payload)
            LOGGER.debug(event_payload)
        elif event.Type == "keypad":
            state_payload = {"state": sensor_state}

            state_topic = f"{CONFIG['self_topic_root']}/{event.MAC}"
            event_topic = f"{CONFIG['self_topic_root']}/{event.MAC}/{event_type}"
            pin_topic = f"{CONFIG['self_topic_root']}/{event.MAC}/pin"
            set_topic = f"{CONFIG['self_topic_root']}/{event.MAC}/set"

            mqtt_publish(state_topic, event_payload)

            if event_type == "mode" and not validate_pin_entry(event.MAC, sensor_state):
                return

            if event_type in ["mode", "motion"]:
                mqtt_publish(event_topic, state_payload)
                mqtt_publish(pin_topic, {'state': False})
                if event_type == "mode" or sensor_state == "inactive":
                    mqtt_publish(set_topic, {'mode': None, 'pin': False})
            elif event_type in ['pinStart', 'pinConfirm']:
                mqtt_publish(pin_topic, state_payload)

            event_payload.update(state_payload)
            LOGGER.debug(event_payload)

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
    SET_TOPIC = f"{CONFIG['self_topic_root']}/+/set"

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
