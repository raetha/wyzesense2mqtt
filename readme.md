# WyzeSense to MQTT Gateway

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


## Installation and Setup

### Docker Way
This is the most highly tested method of running the gateway. It allows for persistance and easy migration assuming the hardware dongle moves along with the configuration. All steps are performed from Docker host, not container.

1. Create a docker compose file similar to the following:
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
2. Create your local volume mounts
```bash
mkdir /docker/wyzesense2mqtt/config
mkdir /docker/wyzesense2mqtt/logs
```
3. Prepare config.yaml file (see sample below or copy from repository)
```bash
vim /docker/wyzesense2mqtt/config/config.yaml
```
4. Prepare logging.yaml file (see sample below or copy from repository)
```bash
vim /docker/wyzesense2mqtt/config/logging.yaml
```
5. If desired, pre-populate a sensors.yaml file with your existing sensors. This file will automatically be created if it doesn't exist. (see sample below or copy from repository)
```bash
vim /docker/wyzesense2mqtt/config/sensors.yaml
```
6. Start up docker container
```bash
docker-compose up -d
```

### Linux Systemd Way

The gateway can also be run as a systemd service for those not wanting to use Docker.
1. Create an application folder
```bash
mkdir /wyzesense2mqtt
cd /wyzesense2mqtt
```
2. Pull down a copy of the repository
```bash
git clone https://github.com/raetha/wyzesense2mqtt.git
```
3. Prepare config.yaml file (see sample below)
```bash
cp /wyzesense2mqtt/config/config.yaml.sample /wyzesense2mqtt/config/config.yaml
vim /wyzesense2mqtt/config/config.yaml
```
4. Modify logging.yaml file if desired (optional)
```bash
vim /wyzesense2mqtt/config/logging.yaml
```
5. If desired, pre-populate a sensors.yaml file with your existing sensors. This file will automatically be created if it doesn't exist. (see sample below)
```bash
vim /wyzesense2mqtt/config/sensors.yaml
```
6. Start the service. Service file only needs to be modified if not using /wyzesense2mqtt as the application path.
```bash
sudo cp /wyzesense2mqtt/wyzesense2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start wyzesense2mqtt
sudo systemctl status wyzesense2mqtt
```


## Config files
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


## Usage
### Adding a new sensor
To add a new sensor, publish a blank message to the MQTT topic "self_topic_root/scan" where self_topic_root is the value from the configuration file. The default MQTT topic would be "wyzesense2mqtt/scan" if you haven't changed the configuration. This can be performed via Home Assistant or any MQTT client.


### Removing a sensor
To remove a sensor, publish a message containing the MAC to be removed to the MQTT topic "self_topic_root/remove" where self_topic_root is the value from the configuration file. The default MQTT topic would be "wyzesense2mqtt/remove" if you haven't changed the configuration. The payload should look like "AABBCCDD". This can be performed via Home Assistant or any MQTT client.


### Using the command line tool
The bridge_tool_cli.py script can be used to interact with your bridge a perform a few simple functions. Make sure to specify the correct device for your environment.
```bash
python3 bridge_tool_cli.py --device /dev/hidraw0
```
Once run it will present a menu of its functions:
* L - List paired sensor MACs
* P - Put into scanning mode to pair new sensors
* U - Unpair the MAC specified after U (e.g. "U AABBCCDD"
* F - Remove a sensor with MAC 00000000, common problem with failing sensors


## Home Assistant
Home Assistant simply needs to be configured with the MQTT broker that the gateway publishes topics to. Once configured, the MQTT integration will automatically add devices for each sensor along with entites for the state, battery_level, and signal_strength. By default these additionals will have a device_class of "opening" for contact sensors and "motion" for motion sensors. They will be named for the sensor type and MAC, e.g. Wyze Sense Contact Sensor AABBCCDD. To adjust the device_class to door or window, and set a custom name, update the sensors.yaml configuration file and replace the defaults.


## Tested
Tested on Alpine Linux (Docker) and Raspbian
