# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [4.0.0] — 2026-06-29

### Breaking changes

**Container image renamed and environment variables updated** — the hub image
is now `ghcr.io/<owner>/ws2m-hub` (was `ghcr.io/<owner>/wyzesense2mqtt`).
Updating to the new image requires editing your compose file; while there,
all environment variables must also be updated to use a `WS2M_` prefix
(e.g. `MQTT_HOST` → `WS2M_MQTT_HOST`). Unprefixed names are no longer
accepted. Updated compose and env file examples are in `examples/hub/`;
Dockerfiles are `docker/Dockerfile.hub` and `docker/Dockerfile.remote`.
The remote image is `ghcr.io/<owner>/ws2m-remote` (new — see Added).

**Data directory renamed** — the default data directory inside the container
has been renamed from `config/` to `data/` (`/app/config` → `/app/data`).
On first start, `service.sh` creates a symlink so existing mounts continue
to work. To migrate cleanly, update your volume mount to `/app/data` and
remove the symlink.

**Logging** — logs now go to stdout only (`docker logs` / `journalctl`).
`config/logging.yaml` and the `logs/` directory are no longer used; remove
the `VOL_LOGS` volume mount from your compose file.

**Default topic prefix and client ID** — `self_topic_root` and
`mqtt_client_id` now default to `ws2m` instead of `wyzesense2mqtt`. Installs
with these values set explicitly are unaffected. If your broker has ACL rules
or HA automations referencing `wyzesense2mqtt`, update them or pin the old
values in your config.

**Sensor data payload attributes** renamed: `wyzesense2mqtt_version` →
`ws2m_version`, `discovery_schema_version` → `ws2m_discovery_schema`. Update
any HA templates or automations that reference these attributes.

**Hub HA device identity** — the single `ws2m_bridge_<mac>` device is now
two devices: `ws2m_hub_<uuid>` (software) and `ws2m_dongle_<mac>`
(hardware). On first 4.0 startup the bridge clears all pre-4.0 retained
topics automatically (schema v1→v2). Delete the stale bridge device from HA's
device registry manually after upgrading.

**Sensor data directory restructured** — sensor files have moved from
`<data>/sensors.yaml` and `<data>/state.yaml` to
`<data>/dongles/<dongle_mac>/sensors.yaml` and `state.yaml`. Existing flat
files are migrated automatically on first start.

**Scan and remove MQTT topics are now dongle-scoped** — `ws2m/scan` and
`ws2m/remove` are now `ws2m/dongle/<mac>/scan` and `ws2m/dongle/<mac>/remove`.

**Python 3.12+ required.**

### Added

- **Remote bridge** — a new `ws2m-remote` image runs on any machine with a
  USB WyzeSense dongle and forwards raw HID frames to the hub over an
  authenticated WebSocket. The hub runs the full sensor protocol; the remote
  is fully transparent. Supports hot-reconnect with a replay buffer (10 s TTL,
  500-frame ring buffer) so brief network disruptions do not drop sensor events.
  Remotes auto-discover the hub via mDNS (`_ws2m._tcp.local.`) when
  `WS2M_HUB_URL` is not set — useful for simple single-hub setups. Set
  `WS2M_HUB_URL` explicitly when crossing VLANs or in Docker without host
  networking. Enable remote connections on the hub with `hub_ws_enabled: true`
  in `config.yaml` or `WS2M_HUB_WS_ENABLED=true` in the environment; the HA
  hub device also exposes a **WebSocket remote listener** switch for toggling
  this at runtime. Activate pairing mode via the `ws2m/hub/<uuid>/remote_pair` button
  in HA to adopt new remotes — no pre-shared secret is required. Each remote
  appears in HA with `health` (healthy/degraded), `connected_dongles`
  (count of relayed dongles), a `Restart` button, and a **Remove remote** button
  that clears all MQTT topics for the remote and its entire dongle and sensor chain.
  See `examples/hub/` and `examples/remote/` for compose and service file examples.
- **Multi-dongle support** — `usb_dongle: auto` (the default) connects to all
  WyzeSense bridge dongles at startup. Each gets its own worker with independent
  sensor registry and dongle-scoped MQTT topics. Explicit paths (`/dev/hidrawN`)
  remain supported.
- **HA device hierarchy** — three-level device tree: `ws2m_hub_<uuid>` →
  `ws2m_dongle_<mac>` → `wyzesense_<mac>`, linked via `via_device` so HA shows
  the full chain. A stable hub UUID is generated on first run and persisted
  to `hub.yaml`.
- **Wyze Sense Keypad v2 (WSKP1)** — publishes arm/disarm mode, motion, and PIN
  events to MQTT. Creates an `alarm_control_panel` and `motion` binary sensor in
  HA. Supports PIN validation and pushes state back to the keypad display/LEDs
  via `CMD_SEND_KEYPAD_EVENT`. See [docs/keypad.md](docs/keypad.md).
- **Wyze Video Doorbell V1 Chime (WCHIME1)** — play button and number entities
  for ring tone (0–255), volume (1–9), and repeat count (1–9). Values persist to
  `sensors.yaml`. Ring tone IDs are undocumented; see
  [docs/protocol.md](docs/protocol.md).
- **HA configuration entities** — sensor settings adjustable live from the HA
  device page: sensor name (text), device class (select), invert state (switch),
  and log level (select, hub device). Keypad adds arm PIN capture and clear
  PINs buttons plus a PIN count sensor. **Cleanup removed dongles** button on the
  hub device removes MQTT topics and data for dongles no longer connected (covers
  both local and remote-relayed dongles). **Cleanup disconnected dongles** button
  on each remote device clears topics and data for that remote's failed/disconnected
  dongles while leaving healthy ones untouched.
- **Home Assistant App** — available via
  [raetha/home-assistant-apps](https://github.com/raetha/home-assistant-apps).
  `service.sh` detects `/data/options.json` and loads config automatically,
  including Mosquitto broker auto-discovery via the Supervisor API.
- **Docker `HEALTHCHECK`** — the bridge writes and periodically touches
  `/tmp/ws2m_healthy` while running; removes it on failure. Container flips
  unhealthy within ~90 s of a dongle failure or process hang.
- **Test suite** — 520 unit and integration tests covering all modules.
  Hardware smoke tests behind `pytest -m dongle`. Run with
  `bash scripts/run_tests.sh`.
- **`cli/mqtt_tool.py`** — MQTT maintenance CLI: `cleanup-discovery` finds
  orphaned HA discovery topics; `remove-dongle <mac>` decommissions a dongle
  and clears all its retained topics and data.
- **`docs/protocol.md`** — complete protocol specification including updated
  battery voltage encoding, die temperature field, corrected climate packet
  offsets, and all sensor type event layouts. Separate
  `docs/contributing_hid_captures.md` for the capture/contribution workflow.
- **Battery voltage sensor** — new `battery_voltage` entity (V) published
  alongside the existing battery percentage for all AON_BATMON-reporting
  sensors. Voltage is exact; percentage is a per-chemistry linear estimate.
- **Chip temperature sensor** — on-chip die temperature (°C) from
  `AON_BATMON:TEMP`, disabled by default, added to all alarm and heartbeat
  events as a diagnostic entity.
- **Probe availability gating** — leak sensor `probe_state` entity is only
  included in HA discovery when `probe_available=True` from the most recent
  event; re-published automatically if probe connectivity changes.
- **`dongle_tool fix` upgraded** — now fetches the actual paired sensor list
  from dongle NVRAM, identifies invalid MACs (all-zero, all-0xFF, non-printable
  ASCII), and surgically removes only those entries. Reports how many valid
  sensors were left untouched.
- **`tools/fuzz_keypad.py`** — systematic protocol fuzzer for contributors.

### Changed

- **Major package refactor** — `wyzesense2mqtt.py` (881-line monolith) replaced
  by a structured package: `config.py`, `sensors.py`, `mqtt.py`,
  `dongle_protocol.py` (renamed from `wyzesense.py`, fully snake_cased),
  `bridge.py`, and `cli/`. Old files removed.
- **MQTT topic restructure** — sensor topics moved to `ws2m/sensor/<mac>/`;
  dongle topics to `ws2m/dongle/<mac>/`; hub service topics to
  `ws2m/hub/<uuid>/` (UUID included for multi-hub support). Old flat-root
  sensor and dongle topics are cleared as retained on first 4.0 start.
- **HA MQTT discovery** upgraded to device-based format
  (`homeassistant/device/wyzesense_<mac>/config` with `components`), supported
  since HA 2024.4. Adds `has_entity_name`, `origin`, `suggested_display_precision`,
  and versioned schema migration tracked in `migrations.yaml`.
- Sensor availability now includes both the sensor's own heartbeat topic and its
  dongle's status topic (`availability_mode: all`).
- Logging rationalised: routine events at `DEBUG`; component name in all log
  records (`ws2m.bridge`, `ws2m.mqtt`, etc.).
- Removed config keys `mqtt_qos`, `mqtt_retain`, `publish_sensor_name` — QoS
  and retain are now hardcoded per message type; silently stripped from
  `config.yaml` on first load.
- Per-sensor `timeout` override removed from `sensors.yaml`; timeouts are now
  type-driven (V1: 8 h, V2: 4 h, Chime: 24 h); silently stripped on first load.

### Fixed

- **Climate signal strength offset corrected** — signal strength was read from
  byte offset 7 of the climate event data; the correct offset is 9. The field at
  offset 7 is an unknown reserved byte. All previous climate RSSI values were
  wrong.
- **Battery interpretation corrected** — the battery byte is `AON_BATMON:BAT >> 3`,
  not a percentage. Correct interpretation: `voltage_V = raw / 32.0`. The previous
  percentage approximation coincidentally produced plausible values for 3V sensors
  near full charge but was meaningless at low charge levels and for 1.5V sensors.
- **USB dongle disconnect** — an `OSError` from the HID read loop was previously
  swallowed silently, leaving the worker spinning indefinitely with no output.
  The error now propagates: the bridge logs it, publishes the dongle and all
  attached sensors offline, saves state, and marks the container unhealthy.
  Remaining healthy workers continue unaffected.
- **`invert_state` re-implemented** — present in `sensors.yaml` since v1.1 but
  dropped from bridge logic in v3.1.0. Now applied correctly: swaps
  `payload_on`/`payload_off` in HA discovery for contact and motion sensors.
- Non-ASCII MAC bytes no longer crash event parsing; decoded via latin-1 with a
  warning.

### Migration notes

Existing `config.yaml`, `sensors.yaml`, and `state.yaml` files are compatible —
no manual changes required. Removed config keys (`mqtt_qos`, `mqtt_retain`,
`publish_sensor_name`, per-sensor `timeout`) are silently stripped on first load.
`migrations.yaml` records `discovery_schema_version: 2` after the v1→v2
migration runs; subsequent starts skip the migration.

## [3.1.0] — 2026-06-13

### Maintenance

- Migrated MQTT client to `paho-mqtt` v2 (`CallbackAPIVersion.VERSION2`),
  updating `on_connect`/`on_disconnect` callback signatures and pinning
  `requirements.txt` to `paho-mqtt >= 2, < 3` (#79).
- Removed the unguarded `MQTT_CLIENT.reconnect()` call in the main loop that
  could crash the bridge; automatic reconnection is now handled via
  `connect_async`, `reconnect_delay_set`, and `loop_start`.
- Bridge now publishes an "online" status on `on_connect`, including on
  reconnects after a dropped connection.
- Replaced `flake8` with `ruff` for linting across the codebase; CI
  enforces both `ruff check` and `ruff format --check`.
- Bumped GitHub Actions dependencies to latest major versions.
- Added automated release workflow: pushes a GitHub Release and versioned
  container images to ghcr.io and Docker Hub on `vX.Y.Z` tags.
- `devel_package.yml` now gates on successful CI before publishing
  the `:devel` image.
- Removed `codeql-analysis.yml`; CodeQL scanning enabled via GitHub's
  default setup in repository security settings.


[4.0.0]: https://github.com/raetha/wyzesense2mqtt/compare/v3.1.0...v4.0.0
[3.1.0]: https://github.com/raetha/wyzesense2mqtt/compare/v3.0.2...v3.1.0
