# WyzeSense to MQTT Gateway

[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](http://makeapullrequest.com)
[![GitHub License](https://img.shields.io/github/license/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/blob/master/LICENSE)
[![GitHub Issues](https://img.shields.io/github/issues/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/issues)
[![GitHub PRs](https://img.shields.io/github/issues-pr/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/pulls)
[![GitHub Release](https://img.shields.io/github/v/release/raetha/wyzesense2mqtt)](https://github.com/raetha/wyzesense2mqtt/releases)
[![Python Validation](https://github.com/raetha/wyzesense2mqtt/workflows/Python%20Validation/badge.svg)](https://github.com/raetha/wyzesense2mqtt/actions?query=workflow%3A%22Python+Validation%22)

Configurable WyzeSense to MQTT Gateway intended for use with Home Assistant or other platforms that use the same MQTT discovery mechanisms. The gateway allows direct local access to [Wyze Sense](https://wyze.com/wyze-sense.html) products without the need for a Wyze Cam or cloud services. This project and its dependencies have no relation to Wyze Labs Inc.

Please submit pull requests against the devel branch.

## Special Thanks
* [HcLX](https://hclxing.wordpress.com) for [WyzeSensePy](https://github.com/HclX/WyzeSensePy), the core library this project forked.
* [Kevin Vincent](http://kevinvincent.me) for [HA-WyzeSense](https://github.com/kevinvincent/ha-wyzesense), the reference code I used to get things working right with the calls to WyzeSensePy.
* [ozczecho](https://github.com/ozczecho) for [wyze-mqtt](https://github.com/ozczecho/wyze-mqtt), the inspiration for this project.

## Table of Contents
- [WyzeSense to MQTT Gateway](#wyzesense-to-mqtt-gateway)
  - [Special Thanks](#special-thanks)
  - [Table of Contents](#table-of-contents)
  - [Installation](#installation)
    - [Docker](#docker)
    - [Home Assistant App](#home-assistant-app)
    - [Linux Systemd](#linux-systemd)
  - [Configuration Files](#configuration-files)
    - [config.yaml](#configyaml)
      - [sensors.yaml](#sensorsyaml)
  - [Usage](#usage)
    - [Pairing a Sensor](#pairing-a-sensor)
    - [Removing a Sensor](#removing-a-sensor)
    - [Reload Sensors](#reload-sensors)
    - [Command Line Tools](#command-line-tools)
      - [Bridge Tool](#bridge-tool)
      - [Maintenance CLI](#maintenance-cli)
  - [Home Assistant](#home-assistant)
  - [Compatible Hardware](#compatible-hardware)

## Installation

### Docker
This is the most tested method of running the gateway. It allows for persistance and easy migration assuming the hardware dongle moves along with the configuration. All steps are performed from the Docker host, not the container. Images are published to GHCR and Docker Hub.

1. Plug the Wyze Sense Bridge into a USB port on the Docker host. Confirm that it shows up as /dev/hidraw0, if not, update the devices entry in the Docker Compose file with the correct device path.
2. Create a Docker Compose file and a .env file similar to the following. See [Docker Compose Docs](https://docs.docker.com/compose/) for more details on the file format and options. Example files for docker-compose.yml and .env are also included in the repository for easy copying.
```yaml
### Example docker-compose.yml ###
services:
  wyzesense2mqtt:
    container_name: wyzesense2mqtt
    hostname: wyzesense2mqtt
    image: ghcr.io/raetha/wyzesense2mqtt:${IMAGE_TAG:-latest}
    network_mode: bridge
    restart: unless-stopped
    tty: true
    stop_signal: SIGINT
    environment:
      TZ: "${TZ:-UTC}"
      WS2M_MQTT_HOST: "${WS2M_MQTT_HOST}"
      WS2M_MQTT_PORT: "${WS2M_MQTT_PORT:-1883}"
      WS2M_MQTT_USERNAME: "${WS2M_MQTT_USERNAME}"
      WS2M_MQTT_PASSWORD: "${WS2M_MQTT_PASSWORD}"
      WS2M_MQTT_CLIENT_ID: "${WS2M_MQTT_CLIENT_ID:-ws2m}"
      WS2M_MQTT_CLEAN_SESSION: "${WS2M_MQTT_CLEAN_SESSION:-false}"
      WS2M_MQTT_KEEPALIVE: "${WS2M_MQTT_KEEPALIVE:-60}"
      WS2M_MQTT_QOS: "${WS2M_MQTT_QOS:-0}"
      WS2M_MQTT_RETAIN: "${WS2M_MQTT_RETAIN:-true}"
      WS2M_SELF_TOPIC_ROOT: "${WS2M_SELF_TOPIC_ROOT:-ws2m}"
      WS2M_HASS_TOPIC_ROOT: "${WS2M_HASS_TOPIC_ROOT:-homeassistant}"
      WS2M_HASS_DISCOVERY: "${WS2M_HASS_DISCOVERY:-true}"
      WS2M_PUBLISH_SENSOR_NAME: "${WS2M_PUBLISH_SENSOR_NAME:-true}"
      WS2M_USB_DONGLE: "${WS2M_USB_DONGLE:-auto}"
      WS2M_LOG_LEVEL: "${WS2M_LOG_LEVEL:-INFO}"
    devices:
      - "${DEV_WYZESENSE:-/dev/hidraw0}:/dev/hidraw0"
    volumes:
      - "${VOL_CONFIG}:/app/data"
```
```shell
### Example .env ###
IMAGE_TAG=latest
TZ=America/New_York
WS2M_MQTT_HOST=
WS2M_MQTT_PORT=1883
WS2M_MQTT_USERNAME=
WS2M_MQTT_PASSWORD=
WS2M_MQTT_CLIENT_ID=ws2m
WS2M_MQTT_CLEAN_SESSION=false
WS2M_MQTT_KEEPALIVE=60
WS2M_MQTT_QOS=0
WS2M_MQTT_RETAIN=true
WS2M_SELF_TOPIC_ROOT=ws2m
WS2M_HASS_TOPIC_ROOT=homeassistant
WS2M_HASS_DISCOVERY=true
WS2M_PUBLISH_SENSOR_NAME=true
WS2M_USB_DONGLE=auto
WS2M_LOG_LEVEL=INFO
DEV_WYZESENSE=/dev/hidraw0
VOL_CONFIG=/docker/wyzesense2mqtt/config
```
3. Create your local volume mounts. Use the same folders you entered in the Docker Compose files created above.
```bash
mkdir /docker/wyzesense2mqtt/config
```
4. (Optional, when using Docker environment variables) Create or copy a config.yaml file into the config folder (see example below or copy from repository). The script will automatically create a default config.yaml if one is not found, but it will need to be modified with the correct MQTT details before things will work.
5. (Optional) Pre-populate a sensors.yaml file for your dongle into `<data>/dongles/<dongle_mac>/sensors.yaml`. This file will automatically be created when sensors are first discovered. The dongle MAC is shown in the startup log.
6. Start the Docker container
```bash
docker-compose up -d
```
7. Pair sensors following [instructions below](#pairing-a-sensor). You do NOT need to re-pair sensors that were already paired, they should be found automatically on start and added to the config file with default values, though the sensor version will be unknown and the class will default to opening, i.e. a contact sensor. You should manually update these entries.

### Home Assistant App

WyzeSense2MQTT is available as a Home Assistant App for HAOS and Supervised
installs via a dedicated app repository. The app auto-discovers the Mosquitto
broker app so no MQTT configuration is needed in most cases.

[![Add to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fraetha%2Fhome-assistant-apps)

Or add the repository URL manually in **Settings → Apps → App Store → ⋮ → Repositories**:
```
https://github.com/raetha/home-assistant-apps
```

See the [home-assistant-apps repository](https://github.com/raetha/home-assistant-apps)
for full installation and configuration documentation.

### Linux Systemd

If you would like to use this project outside of docker, please follow the instructions at [Linux Systemd Installation](docs/linux_systemd_installation.md). This method is not actively tested and may require more knowledge to succesfully implement.

## Configuration Files
The gateway uses three config files located in the config directory. Examples of each are below and in the repository.

### config.yaml
This is the main configuration file. Aside from MQTT host, username, and password, the defaults should work for most people. A working configuration will be created automatically if ENV values are available for at least `mqtt_host`. So it does not need to be created in advance. Use `log_level` to control verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`); default is `INFO`. Logs go to stdout and are captured by `docker logs` or `journalctl` automatically.
```yaml
mqtt_host: <host>
mqtt_port: 1883
mqtt_username: <user>
mqtt_password: <password>
mqtt_client_id: ws2m
mqtt_clean_session: false
mqtt_keepalive: 60
mqtt_qos: 0
mqtt_retain: true
self_topic_root: ws2m
hass_topic_root: homeassistant
hass_discovery: true
publish_sensor_name: true
usb_dongle: auto
log_level: INFO
``` 


### sensors.yaml
This file stores per-sensor configuration for each sensor paired to a Wyze Sense Bridge dongle. In 4.0 and later it lives at `<data>/dongles/<dongle_mac>/sensors.yaml` (one file per dongle). Existing flat `sensors.yaml` files at the data root are migrated automatically on first start. Entries can be modified to set the sensor name and class as they will appear in Home Assistant. The `class` field maps to an HA binary_sensor device class (`opening`, `door`, `window`, `motion`, `moisture`, etc.). A per-sensor availability timeout can be set via `timeout` (in seconds); the default is 28800 s (8 h) for v1 sensors and 14400 s (4 h) for v2 sensors.

Sensors added via the `scan` MQTT command will populate this file automatically with the correct `sensor_type` and `sw_version`. Sensors that were previously paired and auto-discovered will default to `class: opening` and will not have `sw_version` set. For v1 devices `sw_version` is typically `19`; for v2 devices it is typically `23`.

The **Keypad v2** supports an optional `pins` list for PIN validation. If omitted, all PIN entries are treated as valid. See [docs/keypad.md](docs/keypad.md) for full details.

The **Chime** supports optional `ring_id` (0–255, default 0), `volume` (1–9, default 5), and `repeat_count` (1–9, default 1) keys. These can also be adjusted live from the HA device page — changes are written back to this file automatically.

```yaml
'AAAAAAAA':
  class: door
  name: Entry Door
  invert_state: false
  sw_version: 19
'BBBBBBBB':
  class: window
  name: Office Window
  invert_state: false
  sw_version: 23
  timeout: 7200
'CCCCCCCC':
  class: opening
  name: Kitchen Fridge
  invert_state: false
  sw_version: 19
'DDDDDDDD':
  class: motion
  name: Hallway Motion
  invert_state: false
  sw_version: 19
'EEEEEEEE':
  class: moisture
  name: Basement Moisture
  invert_state: true
  sw_version: 19
'KPADKPAD':
  name: Front Door Keypad
  sensor_type: keypad
  pins:
    - "1234"
    - "5678"
'CHIMEMAC':
  name: Front Door Chime
  sensor_type: chime
  ring_id: 0
  volume: 5
  repeat_count: 1
```

## Usage
### Pairing a Sensor
At this time only a single sensor can be properly paired at once. Please repeat the steps below for each sensor.

With multi-dongle support, scan is scoped to a specific dongle. If you only have one dongle the MAC is shown in the startup log.

1. Publish a blank message (payload `scan`) to the MQTT topic `<self_topic_root>/dongle_<dongle_mac>/scan` (e.g. `wyzesense2mqtt/dongle_AABBCCDD/scan`). This can be done via Home Assistant or any MQTT client. With HA discovery enabled, a **Scan for sensor** button appears on each dongle's device page.
2. Use the pin tool that came with your Wyze Sense sensors to press the reset switch on the side of the sensor. Hold until the red LED blinks.

### Removing a Sensor
Remove is also dongle-scoped — the sensor can only be removed from the dongle it is paired with.

1. Publish the sensor MAC address as the payload to the MQTT topic `<self_topic_root>/dongle_<dongle_mac>/remove` (e.g. `wyzesense2mqtt/dongle_AABBCCDD/remove`). The payload should be the 8-character MAC, e.g. `EEFFGGHH`. With HA discovery enabled, a **Remove sensor** button also appears on each sensor's device page.

### Reload Sensors
If you have modified a `sensors.yaml` while the gateway is running, you can trigger a reload of all dongles without restarting the service or Docker container.

1. Publish a blank message (payload `reload`) to the MQTT topic `<self_topic_root>/reload` (default: `wyzesense2mqtt/reload`). With HA discovery enabled, a **Reload config** button appears on the WyzeSense2MQTT service device page.

### Command Line Tools

#### Bridge Tool
`cli/bridge_tool.py` provides direct USB dongle access for pairing, unpairing, listing sensors, and low-level diagnostics. It does **not** require the bridge service or an MQTT broker to be running. Run it from inside the container (`docker exec -it wyzesense2mqtt sh`) or directly on the host.

```bash
# List paired sensors
python3 -m cli.bridge_tool --device /dev/hidraw0 list

# Pair a new sensor (waits up to 60 s)
python3 -m cli.bridge_tool --device /dev/hidraw0 pair

# Unpair a sensor
python3 -m cli.bridge_tool --device /dev/hidraw0 unpair AABBCCDD

# Remove sensors with corrupt/null MACs (common after battery failure)
python3 -m cli.bridge_tool --device /dev/hidraw0 fix

# Monitor live sensor events
python3 -m cli.bridge_tool --device /dev/hidraw0 monitor

# Show help / all available commands
python3 -m cli.bridge_tool --help
```

#### Maintenance CLI
`cli/maintenance.py` is a standalone tool for operating on the MQTT broker. It does not touch the USB dongle and does not require the bridge service to be running. Run it via `docker exec` into a running container, or on the host if the broker is reachable.

`cleanup-discovery` scans for Home Assistant discovery topics belonging to sensors that are no longer in any dongle's `sensors.yaml` (e.g. removed by editing config files directly rather than via the [Removing a Sensor](#removing-a-sensor) MQTT command) and reports them. By default this is a dry run; pass `--apply` to actually clear the orphaned retained topics.

```bash
# Dry run — show what would be cleared
python3 -m cli.maintenance cleanup-discovery

# Actually clear orphaned topics
python3 -m cli.maintenance cleanup-discovery --apply

# Increase listen time if broker is slow to replay retained messages (default: 5 s)
python3 -m cli.maintenance cleanup-discovery --listen-seconds 15
```

See [docs/HA_MQTT_COMPLIANCE.md](docs/HA_MQTT_COMPLIANCE.md) for details on the MQTT discovery format used and how schema migrations/cleanup work.

## Home Assistant
Home Assistant simply needs to be configured with the MQTT broker that the gateway publishes topics to. Once configured, the MQTT integration will automatically add a device for each sensor, along with entities for state, battery, and signal strength (plus temperature/humidity for climate and leak sensors). By default these entities will have a `device_class` of `opening` for contact sensors, `motion` for motion sensors, and `moisture` for leak sensors, and the device will be named `WyzeSense <MAC>`. To adjust the `device_class` to `door` or `window` and set a custom device name, update the `sensors.yaml` configuration file and trigger a [reload](#reload-sensors).

The **Keypad v2** (WSKP1) creates an `alarm_control_panel` entity and a `motion` binary sensor. It is designed to be used with [Alarmo](https://github.com/nielsfaber/alarmo) or HA automations — see [docs/keypad.md](docs/keypad.md) for full setup instructions including entry/exit delay handling and PIN configuration.

The **Wyze Video Doorbell V1 Chime** (WCHIME1) creates a `button` entity to trigger playback and `number` entities for ring tone, volume, and repeat count. These settings are adjustable directly from the HA device page and are persisted to `sensors.yaml` automatically.

Discovery uses Home Assistant's device-based MQTT discovery format (one config topic per device, covering all of its entities). See [docs/HA_MQTT_COMPLIANCE.md](docs/HA_MQTT_COMPLIANCE.md) for the HA version this was last verified against and notes on the discovery schema and migrations.

## Compatible Hardware
### Wyze Branded
* Wyze Sense Bridge (WHSB1)
* Wyze Sense Bridge Sensors
    * Contact Sensor v1
    * Motion Sensor v1
* Wyze Sense Hub Sensors - Requires installing the Wyze Sense Hub firmware onto a Wyze Sense Bridge (unsupported)
    * Entry Sensor v2 (WSES2)
    * Motion Sensor v2 (WSMS2)
    * Climate Sensor (WSCS1)
    * Leak Sensor (WSLS1)
    * Keypad v2 (WSKP1) — See [docs/keypad.md](docs/keypad.md) for setup with Home Assistant and Alarmo
    * Wyze Video Doorbell V1 Chime (WCHIME1) — Partial support; play command and ring tone, volume, and repeat controls are available via HA. Ring tone IDs are undocumented — see [docs/contributing_protocol.md](docs/contributing_protocol.md).

### Neos Smart Branded
* Neos Smart Bridge (N-LSP-US1)
* Neos Smart Sensors - Not tested, but theoretically compatible
    * Contact Sensor
    * Motion Sensor
    * Leak Sensor
