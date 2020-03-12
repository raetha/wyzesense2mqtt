# WyzeSense2Mqtt Gateway

[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](http://makeapullrequest.com)
![GitHub License](https://img.shields.io/github/license/raetha/wyzesense2mqtt)
![GitHub Issues](https://img.shields.io/github/issues/raetha/wyzesense2mqtt)
![GitHub PRs](https://img.shields.io/github/issues-pr/raetha/wyzesense2mqtt)
![GitHub Downloads](https://img.shields.io/github/downloads/raetha/wyzesense2mqtt/total)
![GitHub Release](https://img.shields.io/github/v/release/raetha/wyzesense2mqtt)

Configurable WyzeSense to MQTT Gateway intended for use with Home Assistant or other platforms that use its MQTT discovery mechanisms.

> Special thanks to [HcLX](https://hclxing.wordpress.com) and his work on [WyzeSensePy](https://github.com/HclX/WyzeSensePy) which is the core library this component uses.
> Also to Kevin Vincent (https://github.com/kevinvincent/ha-wyzesense) for his work on HA-WyseSense which this is heavily based on.
> Lastly to ozczecho (https://github.com/ozczecho/wyze-mqtt) for his work on wyze-mqtt which was the original base code for this project.


## Setup

### Docker Way
This is the most highly tested method of running the gateway. It allows for persistance and easy migration assuming the hardware dongle moves along with the configuration.

```bash
# -- Download docker folder
#
# -- Prep docker-compose file
mv docker-compose.yml.sample docker-compose.yml
# Modify docker-compose.yml to fit your environment.
# Particularly config volume and hidraw0 device locations.
#
# -- Download or copy config folder files to config volume
#
# -- Prep config files
mv config.yaml.sample config.yaml
# Edit configuration to your desired settings.
# Default logger settings are in logging.yaml but can be changed if desired.
#
# -- Prep sensors (optional)
# A sample sensors config is in sensors.yaml.sample.
# You can preconfigure your sensors if MACs are known.
# Otherwise the gateway will build this file for you as sensors are found.
#
# -- Build and start Docker container
docker-compose build
docker-compose up -d
```

### Linux Systemd Way

The gateway can be run as a systemd service. Edit the files as required.

```bash
sudo cp wyzesense2mqtt.service /etc/systemd/system/
# Edit the `service` file to suit your requirements
sudo systemctl daemon-reload
sudo systemctl start wyzesense2mqtt
sudo systemctl status wyzesense2mqtt
```


## Config files
The gateway uses three config files located in the config directory. Samples of each are below.

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
This file contains a yaml dictionary for the logging.config module. Python docs at (https://docs.python.org/3/library/logging.config.html)
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
This file will store basic information about each sensor paired to the Wyse Sense Bridge. The entries can be modified to set the class type and sensor name as it will show in Home Assistant.
```yaml
AAAAAAAA:
  class: door
  name: Entry Door
BBBBBBBB:
  class: window
  name: Kitchen Window
CCCCCCCC:
  class: opening
  name: Fridge
DDDDDDDD:
  class: motion
  name: Hallway Motion
```


## Home Assistant

TODO - Update this section
To add sensors to the `Wyze hub`, you need to publish a message from Home Assistant that will trigger the scan on the `wyze-mqtt` wrapper. This is done by publishing to topic defined under `subscribeScanTopic`. If successful the wrapper will respond with the newly added sensors MAC address. Add the following automation in home assisant to receive the newly added MAC addresses
```yaml
- alias: Listen for new Wyze Sense sensors added to hub.
  initial_state: true
  trigger:
    - platform: mqtt
      topic: 'home/wyze/newdevice'
  action:
    - service: persistent_notification.create
      data_template:
        title: Wyze Sense Sensors Discovery
        message: "{{ trigger.payload}}"
```

Some sample configurations in Home Assistant. One is for a door / window sensor and the other is for a motion sensor.
```yaml
binary_sensor:
  - platform: mqtt
    name: A Window
    state_topic: "home/wyze/123456789"
    device_class: "window"
    value_template: "{{ value_json.state }}" 
    payload_on: 1
    payload_off: 0

sensor:
  - platform: mqtt
    name: A Motion
    state_topic: "home/wyze/987654321"
    value_template: "{{ value_json.state }}"  
    force_update: true  
```


## Tested
Tested on Alpine Linux (Docker) and Raspbian
