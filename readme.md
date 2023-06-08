# WyzeSense to MQTT Gateway

[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](http://makeapullrequest.com)
[![GitHub License](https://img.shields.io/github/license/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/blob/master/LICENSE)
[![GitHub Issues](https://img.shields.io/github/issues/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/issues)
[![GitHub PRs](https://img.shields.io/github/issues-pr/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/pulls)
[![GitHub Release](https://img.shields.io/github/v/release/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/releases)
[![Python Validation](https://github.com/raetha/wyzesense2mqtt/workflows/Python%20Validation/badge.svg)](https://github.com/raetha/wyzesense2mqtt/actions?query=workflow%3A%22Python+Validation%22)

[![dockeri.co](https://dockeri.co/image/raetha/wyzesense2mqtt)](https://hub.docker.com/r/raetha/wyzesense2mqtt)

Configurable WyzeSense to MQTT Gateway intended for use with Home Assistant or other platforms that use MQTT discovery mechanisms. The gateway allows direct local access to [Wyze Sense](https://wyze.com/wyze-sense.html) products without the need for a Wyze Cam or cloud services. This project and its dependencies have no relation to Wyze Labs Inc.

## Special Thanks
* [HcLX](https://hclxing.wordpress.com) for [WyzeSensePy](https://github.com/HclX/WyzeSensePy), the core library this project uses.
* [Kevin Vincent](http://kevinvincent.me) for [HA-WyzeSense](https://github.com/kevinvincent/ha-wyzesense), the reference code I used to get things working right with the calls to WyzeSensePy.
* [ozczecho](https://github.com/ozczecho) for [wyze-mqtt](https://github.com/ozczecho/wyze-mqtt), the inspiration for this project.
* [rmoriz](https://roland.io/) for [multiarch-test](https://github.com/rmoriz/multiarch-test), this allowed the Docker Hub Autobuilder to work for multiple architectures including ARM32v7 (Raspberry Pi) and AMD64 (Linux).

## Table of Contents
- [WyzeSense to MQTT Gateway](#wyzesense-to-mqtt-gateway)
  - [Special Thanks](#special-thanks)
  - [Table of Contents](#table-of-contents)
  - [Installation](#installation)
    - [Docker](#docker)
    - [Linux Systemd](#linux-systemd)
  - [Config Files](#config-files)
    - [config.yaml](#configyaml)
    - [logging.yaml](#loggingyaml)
    - [sensors.yaml](#sensorsyaml)
  - [Usage](#usage)
    - [Pairing a Sensor](#pairing-a-sensor)
    - [Removing a Sensor](#removing-a-sensor)
    - [Reload Sensors](#reload-sensors)
    - [Command Line Tool](#command-line-tool)
  - [Sense Keypad](#sense-keypad)
  - [Home Assistant](#home-assistant)
  - [Tested On](#tested-on)

## Installation

### Docker
This is the most highly tested method of running the gateway. It allows for persistance and easy migration assuming the hardware dongle moves along with the configuration. All steps are performed from Docker host, not container.

1. Plug Wyze Sense Bridge into USB port on Docker host. Confirm that it shows up as /dev/hidraw0, if not, update devices entry in Docker Compose file with correct path.
2. Create a Docker Compose file similar to the following. See [Docker Compose Docs](https://docs.docker.com/compose/) for more details on the file format and options. If you would like to help test in development feaures, please change the image to "devel" instead of "latest".
```yaml
version: "3.7"
services:
  wyzesense2mqtt:
    container_name: wyzesense2mqtt
    image: raetha/wyzesense2mqtt:latest
    hostname: wyzesense2mqtt
    restart: always
    tty: true
    stop_signal: SIGINT
    network_mode: bridge
    devices:
      - "/dev/hidraw0:/dev/hidraw0"
    volumes:
      - "/docker/wyzesense2mqtt/config:/wyzesense2mqtt/config"
      - "/docker/wyzesense2mqtt/logs:/wyzesense2mqtt/logs"
    environment:
      TZ: "America/New_York"
```
3. Create your local volume mounts. Use the same folders as selected in the Docker Compose file created above.
```bash
mkdir /docker/wyzesense2mqtt/config
mkdir /docker/wyzesense2mqtt/logs
```
4. Create or copy a config.yaml file into the config folder (see sample below or copy from repository). The script will automatically create a default config.yaml if one is not found, but it will need to be modified with your MQTT details before things will work.
5. Copy a logging.yaml file into the config folder (see sample below or copy from repository). The script will automatically create a default logging.yaml if one does not exist. You only need to modify this if more complex logging is required.
6. If desired, pre-populate a sensors.yaml file into the config folder with your existing sensors. This file will automatically be created if it doesn't exist. (see sample below or copy from repository)
7. Start the Docker container
```bash
docker-compose up -d
```
8. Pair sensors following [instructions below](#pairing-a-sensor). You do NOT need to re-pair sensors that were already paired, they should be found automatically on start and added to the config file with default values, but the sensor version will be unknown.

### Linux Systemd

The gateway can also be run as a systemd service for those not wanting to use Docker. Requires Python 3.6 or newer. You may need to do all commands as root, depending on filesystem permissions.
1. Plug Wyze Sense Bridge into USB port on Linux host.
2. Pull down a copy of the repository
```bash
cd /tmp
git clone https://github.com/raetha/wyzesense2mqtt.git
# Use the below command instead if you want to help test the devel branch
# git clone -b devel https://github.com/raetha/wyzesense2mqtt.git
```
3. Create local application folders (Select a location that works for you, example uses /wyzesense2mqtt)
```bash
mv /tmp/wyzesense2mqtt/wyzesense2mqtt /wyzesense2mqtt
rm -rf /tmp/wyzesense2mqtt
cd /wyzesense2mqtt
mkdir config
mkdir logs
```
4. Prepare config.yaml file. You must set MQTT host parameters! Username and password can be blank if unused. (see sample below)
```bash
cp samples/config.yaml config/config.yaml
vim config/config.yaml
```
5. Modify logging.yaml file if desired (optional)
```bash
cp samples/logging.yaml config/logging.yaml
vim config/logging.yaml
```
6. If desired, pre-populate a sensors.yaml file with your existing sensors. This file will automatically be created if it doesn't exist. (see sample below) (optional)
```bash
cp samples/sensors.yaml config/sensors.yaml
vim config/sensors.yaml
```
7. Install dependencies
```bash
sudo pip3 install -r requirements.txt
```
8. Configure the service
```bash
vim wyzesense2mqtt.service # Only modify if not using default application path
sudo cp wyzesense2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start wyzesense2mqtt
sudo systemctl status wyzesense2mqtt
sudo systemctl enable wyzesense2mqtt # Enable start on reboot
```
9. Pair sensors following [instructions below](#pairing-a-sensor). You do NOT need to re-pair sensors that were already paired, they should be found automatically on start and added to the config file with default values, but the sensor version will be unknown.


## Config Files
The gateway uses three config files located in the config directory. Samples of each are below and in the repository.

### config.yaml
This is the main configuration file. Aside from MQTT host, username, and password, the defaults should work for most people.
```yaml
mqtt_host: <host>
mqtt_port: 1883
mqtt_username: <user>
mqtt_password: <password>
mqtt_client_id: wyzesense2mqtt
mqtt_clean_session: false
mqtt_keepalive: 60
mqtt_qos: 2
mqtt_retain: true
self_topic_root: wyzesense2mqtt
hass_topic_root: homeassistant
hass_discovery: true
publish_sensor_name: true
usb_dongle: auto
``` 

### logging.yaml
This file contains a yaml dictionary for the logging.config module. Python docs at [logging configuration](https://docs.python.org/3/library/logging.config.html)
```yaml
version: 1
formatters:
  simple:
    format: '%(message)s'
  verbose:
    datefmt: '%Y-%m-%d %H:%M:%S'
    format: '%(asctime)s %(levelname)-8s %(name)-15s %(message)s'
handlers:
  console:
    class: logging.StreamHandler
    formatter: simple
    level: DEBUG
  file:
    backupCount: 7
    class: logging.handlers.TimedRotatingFileHandler
    encoding: utf-8
    filename: logs/wyzesense2mqtt.log
    formatter: verbose
    level: INFO
    when: midnight
root:
  handlers:
    - file
    - console
  level: DEBUG
```

### sensors.yaml
This file will store basic information about each sensor paired to the Wyse Sensor Bridge. The entries can be modified to set the class type and sensor name as it will show in Home Assistant. Class types can be automatically filled for `opening`, `motion`, and `moisture`, depending on the type of sensor. Since this file can be automatically generated, Python may automatically quote the MACs or not depending on if they are fully numeric.
```yaml
'AAAAAAAA':
  class: door
  name: Entry Door
  invert_state: false
'BBBBBBBB':
  class: window
  name: Office Window
  invert_state: false
'CCCCCCCC':
  class: opening
  name: Kitchen Fridge
  invert_state: false
'DDDDDDDD':
  class: motion
  name: Hallway Motion
  invert_state: false
'EEEEEEEE':
  class: moisture
  name: Basement Moisture
  invert_state: true
```

## Usage
### Pairing a Sensor
At this time only a single sensor can be properly paired at once. So please repeat steps below for each sensor.
1. Publish a blank message to the MQTT topic "self_topic_root/scan" where self_topic_root is the value from the configuration file. The default MQTT topic would be "wyzesense2mqtt/scan" if you haven't changed the configuration. This can be performed via Home Assistant or any MQTT client.
2. Use the pin tool that came with your Wyze Sense sensors to press the reset switch on the side of the sensor to pair. Hold in until the red led blinks.


### Removing a Sensor
1. Publish a message containing the MAC to be removed to the MQTT topic "self_topic_root/remove" where self_topic_root is the value from the configuration file. The default MQTT topic would be "wyzesense2mqtt/remove" if you haven't changed the configuration. The payload should look like "AABBCCDD". This can be performed via Home Assistant or any MQTT client.


### Reload Sensors
If you've changed your sensors.yaml file while the gateway is running, you can trigger a reload of the sensors.yaml file without restarting the gateway or Docker container.
1. Publish a blank message to the MQTT topic "self_topic_root/reload" where self_topic_root is the value from the configuration file. The default MQTT topic would be "wyzesense2mqtt/reload" if you haven't changed the configuration. This can be performed via Home Assistant or any MQTT client.


### Command Line Tool
The bridge_tool_cli.py script can be used to interact with your bridge to perform a few simple functions. Make sure to specify the correct device for your environment.
```bash
python3 bridge_tool_cli.py --device /dev/hidraw0
```
Once run it will present a menu of its functions:
* L - List paired sensors
* P - Pair new sensors
* U <mac> - Unpair sensor (e.g. "U AABBCCDD")
* F - Fix invalid sensors (Removes sensors with invalid MACs, common problem with broken sensors or low batteries)


### Sense Keypad
After flashing firmware from the [Sense Hub](https://wyze.com/home-security-system-sensors.html) to the original Sensor Bridge, this project can now support the [Sense Keypad](https://wyze.com/wyze-sense-keypad.html) as well! This entails either [dumping firmware from your own Hub](https://github.com/HclX/WyzeHacks/issues/111#issuecomment-824558304) or using firmware that has already been dumped, and then flashing it. Using the [WyzeSenseUpgrade](https://github.com/AK5nowman/WyzeSense) project is recommended, but is considered "use at your own risk". When a keypad is paired with WyzeSense2MQTT, it will send auto-discovery topics which allow control and statuses from within Home Assistant. The entry in `sensors.yaml` for the keypad looks like the following:
```yaml
'FFFFFFFF':
  name: Front Door Keypad
  class: alarm_control_panel
  pin: '0000'
  expose_pin: false
  arm_required: true
  disarm_required: true
  invert_state: false
  delay_time: 60
  arming_time: 60
  trigger_time: 120
  disarm_after_trigger: False
```

- `pin` must be a string (surrounded by single or double quotes) of digits, but can be any length. Can also be a list of PIN strings, which will be validated against.
- `expose_pin` can be either `true` or `false`. If `true`, a `sensor` entity will be exposed to MQTT discovery which contains the most recent PIN entered. The same PIN will also be saved in the MQTT broker.
- `arm_required` can be either `true` or `false`, and determines whether a pin is required to have been entered to arm the keypad.
- `disarm_required` can be either `true` or `false`, and determines whether a pin is required to have been entered to disarm the keypad.
- `invert_state` can be either `true` or `false`, and affects the motion sensor of the keypad, similarly to other supported sensors.
- `delay_time` must be an integer representing the number of seconds for the `pending` state to last before changing to `triggered`.
- `arming_time` must be an integer representing the number of seconds for the `arming` state to last before changing to an 'armed' state (either `armed_home` or `armed_away`).
- `trigger_time` must be an integer representing the number of seconds for the `triggered` state to last before finishing.
- `disarm_after_trigger` can be either `true` or `false`. If `true`, the keypad will change to `disarmed` after the `triggered` state. If `false`, it will return to the previous state.

A detailed explanation of the relationship between `delay_time`, `arming_time`, `trigger_time`, and `disarm_after_trigger` can be found in the [Home Assistant documentation](https://www.home-assistant.io/integrations/manual/#state-machine). Each of the time options can optionally be configured individually for each 'armed' state.

For example, in this configuration:
- `disarmed` will never trigger the alarm
- `armed_home` will be set with no delay
- `armed_away` will give 30 seconds after arming, and 20 seconds to disarm before triggering the alarm
- When triggered, the alarm will last for 4 seconds
```yaml
'ABABABAB':
  name: Entryway Keypad
  class: alarm_control_panel
  pin: '0000'
  expose_pin: false
  arm_required: true
  disarm_required: true
  invert_state: false
  delay_time: 20
  arming_time: 30
  trigger_time: 4
  disarmed:
    trigger_time: 0
  armed_home:
    arming_time: 0
    delay_time: 0
  disarm_after_trigger: false
```


## Home Assistant
Home Assistant simply needs to be configured with the MQTT broker that the gateway publishes topics to. Once configured, the MQTT integration will automatically add devices for each sensor along with entites for the state, battery_level, and signal_strength. By default these entities will have a device_class of "opening" for contact sensors, "motion" for motion sensors, and "moisture" for leak sensors. They will be named for the sensor type and MAC, e.g. Wyze Sense Contact Sensor AABBCCDD. To adjust the device_class to "door" or "window", and set a custom name, update the `sensors.yaml` configuration file and replace the defaults, then restart WyzeSense2MQTT. For a comprehensive list of device classes the Home Assistant recognizes, see the [`binary_sensor` documentation](https://www.home-assistant.io/integrations/binary_sensor/).


## Tested On
* Ubuntu 20.04 (Docker)
* Debian Buster (Docker)
* Raspbian Buster (RPi 4)
