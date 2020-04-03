# WyzeSense to MQTT Gateway
[![GitHub License](https://img.shields.io/github/license/jeblankenship/wyzesense2mqtt)](https://github.com/jeblankenship/wyzesense2mqtt/blob/master/LICENSE)
[![GitHub Issues](https://img.shields.io/github/issues/jeblankenship/wyzesense2mqtt)](https://github.com/jeblankenship/wyzesense2mqtt/issues)
[![GitHub PRs](https://img.shields.io/github/issues-pr/jeblankenship/wyzesense2mqtt)](https://github.com/jeblankenship/wyzesense2mqtt/pulls)

[![dockeri.co](https://dockeri.co/image/docscode/wyzesense2mqtt)](https://hub.docker.com/r/docscode/wyzesense2mqtt)
## This is a fork of [raetha/wyzesense2mqtt](<https://github.com/raetha/wyzesense2mqtt>)

I found this project while attempting to buid a WyzeSense to MQTT bridge for my Garage/Workshop space.  So please give credit to [raeth](https://github.com/raetha). This code is not meant to be a replacement for his hard work, it is simply my working version that I plan to use for testing and/or custimize to my needs.

## Here are the stats for [raetha/wyzesense2mqtt](<https://github.com/raetha/wyzesense2mqtt>)

[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](http://makeapullrequest.com)
[![GitHub License](https://img.shields.io/github/license/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/blob/master/LICENSE)
[![GitHub Issues](https://img.shields.io/github/issues/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/issues)
[![GitHub PRs](https://img.shields.io/github/issues-pr/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/pulls)
[![GitHub Release](https://img.shields.io/github/v/release/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/releases)
[![Python Validation](https://github.com/raetha/wyzesense2mqtt/workflows/Python%20Validation/badge.svg)]()
[![GitHub Downloads](https://img.shields.io/github/downloads/raetha/wyzesense2mqtt/total)]()

[![dockeri.co](https://dockeri.co/image/raetha/wyzesense2mqtt)](https://hub.docker.com/r/raetha/wyzesense2mqtt)

# The following project description and instructions are from [raetha/wyzesense2mqtt](<https://github.com/raetha/wyzesense2mqtt>)

Configurable WyzeSense to MQTT Gateway intended for use with Home Assistant or other platforms that use MQTT discovery mechanisms. The gateway allows direct local access to [Wyze Sense](https://wyze.com/wyze-sense.html) products without the need for a Wyze Cam or cloud services. This project and its dependencies have no relation to Wyze Labs Inc.

## Special Thanks
* [raeth](https://github.com/raetha) for [raetha/wyzesense](<https://github.com/raetha/wyzesense2mqtt>) for which this code is based on. 
* [HcLX](https://hclxing.wordpress.com) for [WyzeSensePy](https://github.com/HclX/WyzeSensePy), the core library this component uses.
* [Kevin Vincent](http://kevinvincent.me) for [HA-WyzeSense](https://github.com/kevinvincent/ha-wyzesense), the refernce code I used to get things working right with the calls to WyzeSensePy.
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
    - [Command Line Tool](#command-line-tool)
  - [Home Assistant](#home-assistant)
  - [Tested On](#tested-on)

## Installation

### Docker
This is the most highly tested method of running the gateway. It allows for persistance and easy migration assuming the hardware dongle moves along with the configuration. All steps are performed from Docker host, not container.

1. Plug Wyze Sense Bridge into USB port on Docker host. Confirm that it shows up as /dev/hidraw0, if not, update devices entry in Docker Compose file with correct path.
2. Create a Docker Compose file similar to the following. See [Docker Compose Docs](https://docs.docker.com/compose/) for more details on the file format and options.
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
4. Create or copy a config.yaml file into the config folder (see sample below or copy from repository)
5. Copy a logging.yaml file into the config folder (see sample below or copy from repository)
6. If desired, pre-populate a sensors.yaml file into the config folder with your existing sensors. This file will automatically be created if it doesn't exist. (see sample below or copy from repository)
7. Start the Docker container
```bash
docker-compose up -d
```
8. Pair sensors following instructions below. You do NOT need to re-pair sensors that were already paired, they should be found automatically on start and added to the config file with default values, but the sensor version will be unknown.

### Linux Systemd

The gateway can also be run as a systemd service for those not wanting to use Docker.
1. Plug Wyze Sense Bridge into USB port on Linux host.
2. Pull down a copy of the repository
```bash
cd /tmp
git clone https://github.com/raetha/wyzesense2mqtt.git
```
3. Create local application folder (Select a location that works for you, example uses /wyzesense2mqtt)
```bash
mv /tmp/wyzesense2mqtt/wyzesense2mqtt /wyzesense2mqtt
rm -rf /tmp/wyzesense2mqtt
cd /wyzesense2mqtt
```
4. Prepare config.yaml file. You must set MQTT host parameters! Username and password can be blank if unused. (see sample below)
```bash
mv config/config.yaml.sample config/config.yaml
vim config/config.yaml
```
5. Modify logging.yaml file if desired (optional)
```bash
vim config/logging.yaml
```
6. If desired, pre-populate a sensors.yaml file with your existing sensors. This file will automatically be created if it doesn't exist. (see sample below)
```bash
mv config/sensors.yaml.sample config/sensors.yaml
vim config/sensors.yaml
```
7. Install dependencies
```bash
pip3 install -r requirements.txt
```
8. Start the service.
```bash
vim wyzesense2mqtt.service # Only modify if not using default application path
sudo cp wyzesense2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start wyzesense2mqtt
sudo systemctl status wyzesense2mqtt
```
9. Pair sensors following instructions below. You do NOT need to re-pair sensors that were already paired, they should be found automatically on start and added to the config file with default values, but the sensor version will be unknown.


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
This file will store basic information about each sensor paired to the Wyse Sense Bridge. The entries can be modified to set the class type and sensor name as it will show in Home Assistant. Since this file can be automatically generated, Python may automatically quote the MACs or not depending on if they are fully numeric.
```yaml
'AAAAAAAA':
  class: door
  name: Entry Door
'BBBBBBBB':
  class: window
  name: Kitchen Window
'CCCCCCCC':
  class: opening
  name: Fridge
'DDDDDDDD':
  class: motion
  name: Hallway Motion
```


## Usage
### Pairing a Sensor
At this time only a single sensor can be properly paired at once. So please repeat steps below for each sensor.
1. Publish a blank message to the MQTT topic "self_topic_root/scan" where self_topic_root is the value from the configuration file. The default MQTT topic would be "wyzesense2mqtt/scan" if you haven't changed the configuration. This can be performed via Home Assistant or any MQTT client.
2. Use the pin tool that came with your Wyze Sense sensors to press the reset switch on the side of the sensor to pair. Hold in until the red led blinks.


### Removing a Sensor
1. Publish a message containing the MAC to be removed to the MQTT topic "self_topic_root/remove" where self_topic_root is the value from the configuration file. The default MQTT topic would be "wyzesense2mqtt/remove" if you haven't changed the configuration. The payload should look like "AABBCCDD". This can be performed via Home Assistant or any MQTT client.


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


## Home Assistant
Home Assistant simply needs to be configured with the MQTT broker that the gateway publishes topics to. Once configured, the MQTT integration will automatically add devices for each sensor along with entites for the state, battery_level, and signal_strength. By default these entities will have a device_class of "opening" for contact sensors and "motion" for motion sensors. They will be named for the sensor type and MAC, e.g. Wyze Sense Contact Sensor AABBCCDD. To adjust the device_class to door or window, and set a custom name, update the sensors.yaml configuration file and replace the defaults, then restart WyzeSense2MQTT.


## Tested On
* Alpine Linux (Docker)
* Raspbian
