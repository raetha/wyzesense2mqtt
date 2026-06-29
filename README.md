[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=for-the-badge)](http://makeapullrequest.com)
[![Release](https://img.shields.io/github/v/release/raetha/wyzesense2mqtt?style=for-the-badge)](https://github.com/raetha/wyzesense2mqtt/releases)
[![Validation](https://img.shields.io/github/actions/workflow/status/raetha/wyzesense2mqtt/ci.yml?label=validation&style=for-the-badge)](https://github.com/raetha/wyzesense2mqtt/actions/workflows/ci.yml)
[![Issues](https://img.shields.io/github/issues/raetha/wyzesense2mqtt?style=for-the-badge)](https://github.com/raetha/wyzesense2mqtt/issues)
[![PRs](https://img.shields.io/github/issues-pr/raetha/wyzesense2mqtt?style=for-the-badge)](https://github.com/raetha/wyzesense2mqtt/pulls)
[![License](https://img.shields.io/github/license/raetha/wyzesense2mqtt?style=for-the-badge)](https://github.com/raetha/wyzesense2mqtt/blob/main/LICENSE)

# WyzeSense to MQTT Gateway

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
    - [Remote Bridge (Docker)](#remote-bridge-docker)
    - [Home Assistant App](#home-assistant-app)
    - [Linux Systemd](docs/linux_systemd_installation.md)
  - [Remote Bridge](#remote-bridge)
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
This is the most tested method of running the gateway. It allows for persistence and easy migration assuming the hardware dongle moves along with the configuration. All steps are performed from the Docker host, not the container. Images are published to `ghcr.io`.

1. Plug the Wyze Sense Bridge into a USB port on the Docker host. Confirm that it shows up as `/dev/hidraw0`; if not, update the `devices` entry in the Docker Compose file with the correct path.
2. Copy [`examples/hub/docker-compose.yml.example`](examples/hub/docker-compose.yml.example) to `docker-compose.yml` and [`examples/hub/.env.example`](examples/hub/.env.example) to `.env` in the same directory. Fill in at minimum `WS2M_MQTT_HOST` and `VOL_DATA`. See [Docker Compose Docs](https://docs.docker.com/compose/) for more details.
3. Create your local volume mount directory using the same path you set for `VOL_DATA`:
```bash
mkdir -p /docker/ws2m-hub/data
```
4. (Optional) Pre-populate a sensors.yaml file for your dongle into `<data>/dongles/<dongle_mac>/sensors.yaml`. This file is created automatically when sensors are first discovered. The dongle MAC is shown in the startup log.
5. Start the container:
```bash
docker compose up -d
```
6. Pair sensors following [instructions below](#pairing-a-sensor). Sensors already paired to the dongle are found automatically on start; they will be added with default values (unknown version, contact sensor class) until updated manually.

**Health monitoring:** The container includes a `HEALTHCHECK` that monitors `/tmp/ws2m_healthy`. ws2m writes and periodically touches this file while running normally, and removes it on dongle failure. The container reports as `unhealthy` within ~90 seconds of a dongle failure or process hang. When unhealthy, check `docker logs ws2m-hub` — the failed dongle and all its sensors will also have been published offline to MQTT for automation triggers.

### Remote Bridge (Docker)

To use a WyzeSense USB dongle on a separate machine from the hub, run the `ws2m-remote` image on that machine. The remote forwards raw USB frames to the hub over an authenticated WebSocket.

**On the hub:** enable the WebSocket listener by setting `hub_ws_enabled: true` in `config.yaml` (or `WS2M_HUB_WS_ENABLED=true` in your hub `.env`). The hub advertises itself via mDNS on the local network by default.

**On the remote machine:**
1. Copy [`examples/remote/docker-compose.yml.example`](examples/remote/docker-compose.yml.example) to `docker-compose.yml` and [`examples/remote/.env.example`](examples/remote/.env.example) to `.env` in the same directory.
2. `WS2M_HUB_URL` is optional if the hub and remote are on the same network segment (auto-discovered via mDNS). Set it explicitly when crossing VLANs or in Docker without host networking: `WS2M_HUB_URL=ws://192.168.1.10:8765`.
3. Start the remote: `docker compose up -d`
4. Adopt the remote: see [Adopting a Remote](#adopting-a-remote) below.

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

For hub and remote installations without Docker, see [docs/linux_systemd_installation.md](docs/linux_systemd_installation.md). This method is not actively tested.

## Remote Bridge

The remote bridge lets the hub run on one machine (e.g. your Docker host or Home Assistant server) while the USB dongle lives on a different machine (e.g. a Raspberry Pi in another room). The **remote** (`ws2m-remote` image) forwards raw HID frames to the hub over an authenticated WebSocket.

### How it works

1. The **hub** runs with `hub_ws_enabled: true`. The hub advertises itself via mDNS (`_ws2m._tcp.local.`) so remotes on the same network segment can connect without any explicit URL configuration.
2. The **remote** connects to the hub — automatically via mDNS, or explicitly via `WS2M_HUB_URL`. On first start it generates a stable UUID and attempts to adopt with the hub.
3. **Adoption** — press the **Enable Remote Pairing** button on the hub device in HA (or publish any payload to `<self_topic_root>/hub/<uuid>/remote_pair`). The hub enters pairing mode for `hub_remote_pairing_seconds` seconds; the `<self_topic_root>/hub/<uuid>/remote_pairing` state shows `active`. The next unauthenticated remote that connects receives a unique token, saved on both sides.
4. On subsequent connects the remote presents its token; the hub validates it. No further adoption steps are needed.
5. On reconnect, the remote replays a ring buffer of recent frames (10-second TTL, 500-frame capacity) so brief network disruptions do not lose sensor events.

### Hub configuration

Enable and tune the WebSocket listener in your hub's `config.yaml` (or via ENV vars):

```yaml
hub_ws_enabled: true              # enable WebSocket listener for remote connections
hub_ws_port: 8765                 # WebSocket listener port (default 8765)
hub_remote_pairing_seconds: 60    # how long pairing mode stays active (default 60)
hub_ws_mdns: true                 # advertise via mDNS for auto-discovery (default true)
```

These settings can also be toggled live from the hub device page in Home Assistant.

### Adopting a remote

1. Start the remote container on the machine with the USB dongle.
2. In HA, go to the **WyzeSense Hub** device page and press **Enable Remote Pairing**. The pairing state sensor shows `active` for 60 seconds (or your configured duration).
3. The remote is adopted automatically: its token is saved on both sides. Future restarts reconnect without any further action.

### Remote environment variables

| Variable | Default | Description |
|---|---|---|
| `WS2M_HUB_URL` | *(auto)* | Hub WebSocket URL, e.g. `ws://192.168.1.10:8765`. Optional when hub and remote are on the same network and mDNS is available. |
| `WS2M_DEVICE` | `auto` | HID device path, e.g. `/dev/hidraw0`. `auto` detects all matching dongles. |
| `WS2M_REMOTE_ID` | *(from data dir)* | Override the remote's stable UUID. |
| `WS2M_DATA_DIR` | `/app/data` | Directory for persistent state (`remote_id`, `hub_token`). |
| `WS2M_HUB_ID` | *(none)* | Preferred hub UUID when multiple hubs are discoverable via mDNS. |
| `WS2M_DISCOVERY_TIMEOUT` | `30` | mDNS discovery timeout in seconds. |

### HA entities (remote)

Each adopted remote appears in HA as a **WyzeSense Remote `<UUID>`** device linked to the hub. The device shows as **available** (online) when the ws2m-remote service is connected to the hub, and **unavailable** (offline) when the WebSocket connection drops — this is the remote's connectivity indicator, mirroring how the hub device's availability tracks its MQTT connection. The remote device includes:
- **Health** — `healthy` / `degraded` based on the remote process's self-reported health.
- **Connected dongles** — count of WyzeSense dongles currently being relayed by this remote.
- **Restart** — button to restart the remote container.
- **Remove remote** — button that clears all MQTT topics for this remote and its entire dongle and sensor chain, and deletes the remote's token so it cannot reconnect without re-pairing. Use this after permanently decommissioning a remote so HA removes all related devices cleanly.
- **Cleanup disconnected dongles** — button that clears MQTT topics and data for dongles relayed by this remote that have failed or disconnected, leaving healthy dongles untouched. Use this when a remote's dongle was physically removed but the remote itself is still running.

Each dongle relayed by a remote appears as its own **WyzeSense Dongle `<MAC>`** device in HA — the same device type as locally-attached dongles — with the same set of entities: **Connection state** (online/offline), **Scan for sensor**, and **Remove sensor**. Hub health is independent of remote health.

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
hub_ws_enabled: false
hub_ws_port: 8765
hub_remote_pairing_seconds: 60
hub_ws_mdns: true
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
| `hass_discovery` | `true` | Publish HA MQTT discovery config. When false, ws2m clears all retained discovery config topics on startup. All `ws2m/` state and data topics continue to function normally — only the `homeassistant/` discovery payloads are suppressed. |
| `usb_dongle` | `auto` | USB dongle path. `auto` detects all connected dongles automatically; `/dev/hidrawN` pins to one specific device. |
| `hub_ws_enabled` | `false` | Enable the WebSocket listener to accept connections from `ws2m-remote` instances. |
| `hub_ws_port` | `8765` | WebSocket listener port for remote connections. |
| `hub_remote_pairing_seconds` | `60` | How long remote pairing mode remains active after pressing Enable Remote Pairing. |
| `hub_ws_mdns` | `true` | Advertise the hub via mDNS so remotes can auto-discover without `WS2M_HUB_URL`. |
| `log_level` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. Also adjustable live from the hub device page in HA. |



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

1. Publish a blank message (payload `scan`) to the MQTT topic `<self_topic_root>/dongle/<dongle_mac>/scan` (e.g. `ws2m/dongle/AABBCCDD/scan`). This can be done via Home Assistant or any MQTT client. With HA discovery enabled, a **Scan for sensor** button appears on each dongle's device page.
2. Use the pin tool that came with your Wyze Sense sensors to press the reset switch on the side of the sensor. Hold until the red LED blinks.

### Removing a Sensor
Remove is also dongle-scoped — the sensor can only be removed from the dongle it is paired with.

1. Publish the sensor MAC address as the payload to `<self_topic_root>/dongle/<dongle_mac>/remove` (e.g. `ws2m/dongle/AABBCCDD/remove`). The payload should be the 8-character MAC, e.g. `AABBCCDD`. With HA discovery enabled, a **Remove sensor** button also appears on each sensor's device page.

### Reload Sensors
If you have modified a `sensors.yaml` while the gateway is running, you can trigger a reload of all dongles without restarting the service or Docker container.

1. Publish a blank message (payload `reload`) to `<self_topic_root>/hub/<uuid>/reload`. With HA discovery enabled, a **Reload config** button appears on the WyzeSense Hub device page in HA.

### Removing a Dongle
If a dongle is permanently removed (replaced, retired, or lost), ws2m retains its data directory and HA entities until you explicitly clean them up. A simple restart or USB glitch will not trigger cleanup — the data is preserved for recovery.

**From Home Assistant:** A **Cleanup removed dongles** button appears on the WyzeSense2MQTT hub device page (under the **Configuration** entity category, not the default dashboard view). Pressing it compares the data directories on disk against the currently-connected dongles. Any dongle no longer connected has its retained MQTT discovery and status topics cleared and its `data/dongles/<mac>/` directory deleted. The operation is idempotent — if all known dongles are connected it does nothing.

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
    * Wyze Video Doorbell V1 Chime (WCHIME1) — Partial support; play command and ring tone, volume, and repeat controls are available via HA. Ring tone IDs are undocumented — see [docs/protocol.md](docs/protocol.md).

### Neos Smart Branded
* Neos Smart Bridge (N-LSP-US1)
* Neos Smart Sensors - Not tested, but theoretically compatible
    * Contact Sensor
    * Motion Sensor
    * Leak Sensor
