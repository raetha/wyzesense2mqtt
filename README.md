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
    - [Removing a Dongle](#removing-a-dongle)
    - [Reload Sensors](#reload-sensors)
    - [CLI Tools](docs/cli_tools.md)
  - [Home Assistant](#home-assistant)
  - [Compatible Hardware](#compatible-hardware)

## Installation

### Docker
This is the most tested method of running the gateway. It allows for persistance and easy migration assuming the hardware dongle moves along with the configuration. All steps are performed from the Docker host, not the container. Images are published to GHCR and Docker Hub.

1. Plug the Wyze Sense Bridge into a USB port on the Docker host. Confirm that it shows up as /dev/hidraw0, if not, update the devices entry in the Docker Compose file with the correct device path.
2. Copy [`examples/docker-compose.yml.example`](examples/docker-compose.yml.example) to `docker-compose.yml` and [`examples/.env.example`](examples/.env.example) to `.env` in the same directory. Fill in at minimum `WS2M_MQTT_HOST` and `VOL_DATA`. See [Docker Compose Docs](https://docs.docker.com/compose/) for more details on the file format.
3. Create your local volume mount directory. Use the same path you set for `VOL_DATA` in your `.env` file.
```bash
mkdir -p /docker/wyzesense2mqtt/data
```
4. (Optional) Pre-populate a sensors.yaml file for your dongle into `<data>/dongles/<dongle_mac>/sensors.yaml`. This file will automatically be created when sensors are first discovered. The dongle MAC is shown in the startup log.
5. Start the Docker container
```bash
docker-compose up -d
```
6. Pair sensors following [instructions below](#pairing-a-sensor). You do NOT need to re-pair sensors that were already paired, they should be found automatically on start and added to the config file with default values, though the sensor version will be unknown and the class will default to opening, i.e. a contact sensor. You should manually update these entries.

**Health monitoring:** The container includes a `HEALTHCHECK` that monitors `/tmp/ws2m_healthy`. ws2m writes and periodically touches this file while running normally, and removes it if a dongle fails. The container will report as `unhealthy` within ~90 seconds of a dongle failure or process hang. When unhealthy, check `docker logs wyzesense2mqtt` — the failed dongle and all its sensors will also have been published offline to MQTT for automation triggers.

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
self_topic_root: ws2m
hass_topic_root: homeassistant
hass_discovery: true
usb_dongle: auto
log_level: INFO
```

| Key | Default | Notes |
|---|---|---|
| `mqtt_host` | *(required)* | MQTT broker hostname or IP |
| `mqtt_port` | `1883` | Broker port |
| `mqtt_username` / `mqtt_password` | *(none)* | Broker credentials |
| `mqtt_client_id` | `ws2m` | MQTT client identifier |
| `mqtt_clean_session` | `false` | Persist subscriptions across restarts |
| `mqtt_keepalive` | `60` | Broker keepalive in seconds |
| `self_topic_root` | `ws2m` | Topic prefix for all ws2m data topics. Change when running multiple instances on the same broker. |
| `hass_topic_root` | `homeassistant` | HA MQTT discovery prefix. Only change if you have set `mqtt: discovery_prefix` in HA's `configuration.yaml`. |
| `hass_discovery` | `true` | Publish HA MQTT discovery config. Disable to suppress all discovery; ws2m cleans up retained topics on startup when false. |
| `usb_dongle` | `auto` | USB dongle path. `auto` detects all connected dongles; set to `/dev/hidrawN` to pin to a specific device. |
| `log_level` | `INFO` | Log verbosity. `DEBUG`, `INFO`, `WARNING`, or `ERROR`. Also adjustable live from the HA service device page. |



### sensors.yaml
This file stores per-sensor configuration for each sensor paired to a Wyze Sense Bridge dongle. In 4.0 and later it lives at `<data>/dongles/<dongle_mac>/sensors.yaml` (one file per dongle). Existing flat `sensors.yaml` files at the data root are migrated automatically on first start. Entries can be modified to set the sensor name, class, and invert_state as they will appear in Home Assistant. The `class` field maps to an HA binary_sensor device class (`opening`, `door`, `window`, `motion`, `moisture`, etc.). Availability timeouts are determined automatically by sensor type (8 h for V1, 4 h for V2, 24 h for chime) and are not user-configurable.

Many sensor settings can also be adjusted live from the Home Assistant device page without editing this file — changes are written back automatically.

Sensors added via the `scan` MQTT command will populate this file automatically with the correct `sensor_type` and `sw_version`. Sensors that were previously paired and auto-discovered will default to `class: opening` and will not have `sw_version` set. For v1 devices `sw_version` is typically `19`; for v2 devices it is typically `23`.

The **Keypad v2** supports an optional `pins` list for PIN validation. If omitted, all PIN entries are treated as valid. See [docs/keypad.md](docs/keypad.md) for full details.

The **Chime** supports optional `ring_id` (0–255, default 0), `volume` (1–9, default 5), and `repeat_count` (1–9, default 1) keys. These can also be adjusted live from the HA device page — changes are written back to this file automatically.

```yaml
'AAAAAAAA':
  name: Entry Door
  sensor_type: switchv2
  class: door
  invert_state: false
  sw_version: 23
'BBBBBBBB':
  name: Office Window
  sensor_type: switchv2
  class: window
  invert_state: false
  sw_version: 23
'CCCCCCCC':
  name: Kitchen Fridge
  sensor_type: switch
  class: opening
  invert_state: true      # contact reads closed when door is open — swap payloads
  sw_version: 19
'DDDDDDDD':
  name: Hallway Motion
  sensor_type: motionv2
  class: motion
  invert_state: false
  sw_version: 23
'EEEEEEEE':
  name: Basement Leak
  sensor_type: leak
  sw_version: 23
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

1. Publish a blank message (payload `scan`) to the MQTT topic `<self_topic_root>/dongle_<dongle_mac>/scan` (e.g. `ws2m/dongle_AABBCCDD/scan`). This can be done via Home Assistant or any MQTT client. With HA discovery enabled, a **Scan for sensor** button appears on each dongle's device page.
2. Use the pin tool that came with your Wyze Sense sensors to press the reset switch on the side of the sensor. Hold until the red LED blinks.

### Removing a Sensor
Remove is also dongle-scoped — the sensor can only be removed from the dongle it is paired with.

1. Publish the sensor MAC address as the payload to the MQTT topic `<self_topic_root>/dongle_<dongle_mac>/remove` (e.g. `ws2m/dongle_AABBCCDD/remove`). The payload should be the 8-character MAC, e.g. `EEFFGGHH`. With HA discovery enabled, a **Remove sensor** button also appears on each sensor's device page.

### Reload Sensors
If you have modified a `sensors.yaml` while the gateway is running, you can trigger a reload of all dongles without restarting the service or Docker container.

1. Publish a blank message (payload `reload`) to the MQTT topic `<self_topic_root>/reload` (default: `ws2m/reload`). With HA discovery enabled, a **Reload config** button appears on the WyzeSense2MQTT service device page.

### Removing a Dongle
If a dongle is permanently removed (replaced, retired, or lost), ws2m retains its data directory and HA entities until you explicitly clean them up. A simple restart or USB glitch will not trigger cleanup — the data is preserved for recovery.

**From Home Assistant:** A **Cleanup removed dongles** button appears on the WyzeSense2MQTT service device page (under the **Configuration** entity category, not the default dashboard view). Pressing it compares the data directories on disk against the currently-connected dongles. Any dongle no longer connected has its retained MQTT discovery and status topics cleared and its `data/dongles/<mac>/` directory deleted. The operation is idempotent — if all known dongles are connected it does nothing.

> **Note:** Only press this button after a deliberate permanent removal. If a dongle is temporarily disconnected or experiencing a USB fault, wait until it is reconnected before using this button to avoid losing its sensor configuration.

For surgical single-dongle removal from the command line, see [CLI Tools](docs/cli_tools.md).

### CLI Tools

For situations requiring direct dongle access or surgical MQTT cleanup outside the normal service workflow — such as pairing sensors without the bridge running, diagnosing a dongle the bridge cannot open, or clearing orphaned HA discovery topics — see [docs/cli_tools.md](docs/cli_tools.md).

## Home Assistant
Home Assistant simply needs to be configured with the MQTT broker that the gateway publishes topics to. Once configured, the MQTT integration will automatically add a device for each sensor, along with entities for state, battery, and signal strength (plus temperature/humidity for climate and leak sensors). By default these entities will have a `device_class` of `opening` for contact sensors, `motion` for motion sensors, and `moisture` for leak sensors, and the device will be named `WyzeSense <MAC>`. The following settings are adjustable live from the HA sensor device page and are written back to `sensors.yaml` automatically:

- **Sensor name** — renames the HA device and updates discovery
- **Device class** — for contact sensors: `door`, `window`, `opening`, `garage_door`, `lock`; for motion sensors: `motion`, `occupancy`
- **Invert state** — swaps `payload_on`/`payload_off` in HA discovery, useful for sensors installed in a non-standard orientation (e.g. a contact sensor in a doorbell chime box)

These can also be set directly in `sensors.yaml` and applied via [Reload](#reload-sensors).

The **Keypad v2** (WSKP1) creates an `alarm_control_panel` entity, a `motion` binary sensor, and PIN management entities (`PIN count` sensor, `Arm PIN capture` button, `Clear all PINs` button). To add a PIN: press **Arm PIN capture** in HA, then enter the PIN on the physical keypad — ws2m captures it and adds it to the configured list automatically. See [docs/keypad.md](docs/keypad.md) for full setup instructions including entry/exit delay handling.

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
