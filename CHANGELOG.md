# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [4.0.0] — TBD

### Breaking changes

**Data directory renamed** — the default data directory inside the container
has been renamed from `config/` to `data/` (i.e. `/app/config` →
`/app/data`). On first start, `service.sh` automatically creates a symlink
`/app/data → /app/config` if an existing `/app/config` bind mount is
detected, so existing Docker Compose installs continue to work without
changes. To migrate cleanly, update your volume mount from `/app/config` to
`/app/data` and remove the symlink. The `WS2M_DATA_DIR` environment variable
can override the path entirely.

**Environment variable prefix** — all ws2m-specific environment variables
now use a `WS2M_` prefix (e.g. `MQTT_HOST` → `WS2M_MQTT_HOST`,
`LOG_LEVEL` → `WS2M_LOG_LEVEL`). Unprefixed names are still accepted in
4.0 for backwards compatibility but are deprecated and will be removed in
a future release. Update your `.env` files and Docker Compose environment
blocks — see the updated examples in `examples/`.

**Logging** — logs now go to stdout only (`docker logs` / `journalctl`).
The `logs/` directory and `config/logging.yaml` are no longer used.
Control verbosity with `log_level` in `config.yaml` or the `LOG_LEVEL`
environment variable (default: `INFO`).  Remove the `VOL_LOGS` volume
mount from your compose file (or leave it mounted harmlessly).

**Default topic prefix and client ID** — `self_topic_root` and
`mqtt_client_id` now default to `ws2m` instead of `wyzesense2mqtt`.
Existing installs with either value set explicitly in `config.yaml` or
environment variables are unaffected.  New installs use `ws2m/` as the
MQTT topic prefix.  If your broker has ACL rules or HA automations
referencing `wyzesense2mqtt`, either update them or pin the old values
in your config.

**Sensor data payload attributes** renamed: `wyzesense2mqtt_version` →
`ws2m_version`, `discovery_schema_version` → `ws2m_discovery_schema`.
Update any HA templates or automations that reference these attributes.

**Bridge HA device identity** — the single `ws2m_bridge_<mac>` device
has been split into two: a software service device (`ws2m_service_<uuid>`)
and a hardware dongle device (`ws2m_dongle_<mac>`).  Scan and remove
buttons move to the dongle device; reload stays on the service device.
On first 4.0 startup the bridge automatically clears all pre-4.0 retained
topics in a single migration step (discovery schema v1→v2).
Delete the stale bridge device from HA's device registry manually after
upgrading — the new service and dongle devices will already be present.
Individual sensor devices (`wyzesense_<mac>`) are unchanged.

**Sensor data directory restructured** — per-sensor config and state
files have moved from `<data>/sensors.yaml` and `<data>/state.yaml` to
`<data>/dongles/<dongle_mac>/sensors.yaml` and `state.yaml`.  On first
start, existing flat files are automatically migrated into the correct
per-dongle directory, so no manual action is needed for single-dongle
installs.

**Scan and remove MQTT topics are now dongle-scoped** — previously
`ws2m/scan` and `ws2m/remove`; now `ws2m/dongle_<mac>/scan` and
`ws2m/dongle_<mac>/remove`.  Update any direct MQTT automations that
used the old global scan/remove topics.

**Python 3.12+ required** — 4.0 uses `X | Y` union type hint syntax
not available in earlier versions.  The Docker image now uses
`python:3.12-alpine`.

### Added

- **Multi-dongle support** — when `usb_dongle: auto` is set (the
  default), ws2m detects and connects to all WyzeSense bridge dongles
  present at startup.  Each dongle gets its own `DongleWorker` (owning
  an independent `SensorRegistry` and dongle-scoped MQTT topics), while
  a single `Bridge` orchestrates the shared MQTT connection and publishes
  a software-level service device to HA.  Explicit device paths
  (`/dev/hidrawN`) remain supported for single-dongle installs.
- **HA device hierarchy** — three-level device tree: `ws2m_service_<uuid>`
  (software) → `ws2m_dongle_<mac>` (hardware) → `wyzesense_<mac>` (sensor).
  Service and dongle are `via_device` linked so HA shows the full chain.
- **Stable service UUID** — generated on first run and persisted to
  `service.yaml`, ensuring each ws2m instance has a unique identity on
  the MQTT broker even when multiple instances share the same broker.
- **Sensor availability chain** — each sensor's availability now includes
  both its own heartbeat topic and its dongle's status topic
  (`availability_mode: all`), so a sensor goes unavailable if either its
  specific dongle or the service goes offline.
- **Legacy data migration** — on first start with 4.0, any existing flat
  `sensors.yaml` and `state.yaml` at the data root are automatically
  moved into `dongles/<dongle_mac>/` so single-dongle users are
  unaffected without manual action.
- **Wyze Sense Keypad v2 (WSKP1) support** — full bridge support for the
  HMS keypad.  Publishes arm/disarm mode, motion, and PIN events to MQTT.
  Creates an `alarm_control_panel` entity and a `motion` binary sensor in
  HA via discovery.  PIN validation against a configurable list in
  `sensors.yaml`.  Sends `CMD_SEND_KEYPAD_EVENT` to update the keypad
  display/LEDs when HA or Alarmo pushes a new alarm state.  See
  [docs/keypad.md](docs/keypad.md) for full setup instructions including
  Alarmo integration and entry/exit delay handling.
- **Wyze Video Doorbell V1 Chime (WCHIME1) partial support** — wires up
  `CMD_PLAY_CHIME` (protocol support originally added by HclX) to HA
  discovery.  Creates a `button` entity (play), and `number` entities for
  ring tone (0–255), volume (1–9), and repeat count (1–9).  Number values
  are adjustable from the HA device page and persisted to `sensors.yaml`
  automatically.  Ring tone IDs and their sounds are undocumented; see
  [docs/contributing_protocol.md](docs/contributing_protocol.md) for how
  to explore and report ring ID mappings.
- **`docs/keypad.md`** — setup guide for the Wyze Sense Keypad covering
  MQTT topics, HA discovery entities, PIN configuration, Alarmo
  integration (entry/exit delays, code validation), and manual HA
  automation examples.
- **`docs/contributing_protocol.md`** — guide for contributors wanting
  to capture HID traffic, add support for new packet types, or help
  identify unknowns (e.g. keypad display feedback behaviour, chime ring
  tone IDs).  Covers `capture_hid.py`, `bridge_tool monitor`, packet
  framing reference, and what to include in a bug report.
- **`tools/fuzz_keypad.py`** — systematic protocol fuzzer for exploring
  unknown dongle commands (Pass 1: cmd_id sweep; Pass 2: payload sweep).
  Skips known-dangerous commands, probes dongle health between attempts,
  and logs every packet sent for post-hoc analysis.
- **Home Assistant App repository** — WyzeSense2MQTT is available as a
  Home Assistant App for HAOS and Supervised installs via
  [raetha/home-assistant-apps](https://github.com/raetha/home-assistant-apps).
  The standard Docker image now supports the HA App runtime directly —
  `service.sh` detects `/data/options.json` and loads configuration from
  it automatically, including Mosquitto broker auto-discovery via the
  Supervisor services API. See the README for installation instructions.
- **Test suite** (`tests/`) — 332 unit and integration tests covering
  `config.py`, `sensors.py`, `mqtt.py`, `dongle_protocol.py`, and
  `bridge.py` event/availability/command logic.  Hardware smoke tests
  for the USB dongle behind a `pytest -m dongle` marker.  A synthetic
  HID capture fixture enables protocol regression tests without hardware.
  Run with `bash scripts/run_tests.sh`; see `tests/fixtures/README.md`
  and `tools/capture_hid.py` for fixture capture instructions.
- **`scripts/run_tests.sh`** — creates a `.venv/` automatically on first
  run, then runs lint and the test suite.  Accepts `--coverage`,
  `--hardware [--dongle PATH]`, `-k`, `-x`, `-v` flags.
- **`cli/maintenance.py`** — MQTT maintenance CLI with a
  `cleanup-discovery` command for finding and clearing orphaned HA
  discovery topics (dry-run by default, `--apply` to clear).
- **`tools/capture_hid.py`** — standalone HID frame capture script
  (bridge must not be running); prompts for MAC obfuscation before saving.
- **HA configuration entities** — sensor settings are now adjustable live
  from the Home Assistant device page without editing `sensors.yaml`:
  - **Sensor name** (`text`) — renames the HA device and triggers
    re-discovery so the new name appears immediately.
  - **Device class** (`select`) — contact sensors: `door`, `window`,
    `opening`, `garage_door`, `lock`; motion sensors: `motion`,
    `occupancy`.  Other sensor types have a fixed class.
  - **Invert state** (`switch`) — swaps `payload_on`/`payload_off` in
    the HA discovery config for contact and motion sensors, useful for
    sensors installed in a non-standard orientation.  The raw MQTT data
    topic is unchanged; only HA's interpretation of the value flips.
    Leak sensors are excluded (their `moisture` class and `wet`/`dry`
    payloads already express the correct semantic meaning).
  - **Arm PIN capture** (`button`, keypad only) — arms ws2m to capture
    the next PIN entry from the physical keypad hardware.  The captured
    PIN is added to the `sensors.yaml` pins list automatically.
  - **Clear all PINs** (`button`, keypad only) — removes all configured
    PINs; after clearing, all PIN entries are treated as valid.
  - **PIN count** (`sensor`, keypad only) — read-only count of configured
    PINs; updates after add/clear operations.
  - **Log level** (`select`, service device) — changes the bridge log
    verbosity live (`DEBUG`/`INFO`/`WARNING`/`ERROR`), persists to
    `config.yaml`, and takes effect immediately without a restart.
  All changes are persisted to `sensors.yaml` / `config.yaml` and
  echoed back to the MQTT state topic so HA stays in sync.
- **`invert_state` re-implemented** — the `invert_state` field was
  present in `sensors.yaml` since v1.1 but was inadvertently dropped
  from the bridge logic in v3.1.0 (PR #81).  It is now re-implemented:
  when `true`, the binary sensor's `payload_on` and `payload_off` are
  swapped in the HA discovery payload.  Pre-existing `sensors.yaml`
  entries with `invert_state: true` are respected automatically on 4.0
  startup.  Applicable to contact and motion sensors; ignored for leak
  sensors.
- **`mqtt_qos` and `mqtt_retain` removed** — these config keys are no
  longer user-configurable.  Values are now hardcoded per message type
  (status/discovery: QoS 1 retained; data: QoS 0 not retained; commands:
  QoS 1 not retained; number states: QoS 1 retained).  Existing
  `config.yaml` files with these keys are silently cleaned up on first
  4.0 startup.
- **`publish_sensor_name` removed** — the sensor name was already
  published as the HA device name via MQTT discovery in 4.0.  The config
  key was a no-op and has been removed.  Existing `config.yaml` files
  with this key are silently cleaned up.
- **Per-sensor `timeout` override removed** — availability timeouts are
  now determined entirely by sensor type (V1: 8 h, V2: 4 h, Chime: 24 h)
  and are not user-configurable.  Existing `sensors.yaml` files with a
  `timeout` key have it silently removed on first load.
- **HA MQTT discovery** — upgraded to the device-based format
  (`homeassistant/device/wyzesense_<mac>/config` with `components`),
  supported since HA 2024.4.  Adds `has_entity_name`, `origin`, and
  `suggested_display_precision`.  See `docs/HA_MQTT_COMPLIANCE.md`.
- **Versioned discovery schema migration** — clears stale retained topics
  automatically on upgrade; tracked in `config/migrations.yaml`.

### Changed

- **Major package refactor** — `wyzesense2mqtt.py` (881-line monolith
  with module-level globals) replaced by a structured package:
  - `config.py` — config loading, YAML I/O, path helpers, migration
    tracking, dongle auto-detection.
  - `sensors.py` — `SensorRegistry` class; unified `SENSOR_TYPES`
    registry (replaces three separate lookup tables).
  - `mqtt.py` — `MqttGateway` class; per-sensor-type and bridge discovery
    component builders (factory functions returning fresh dicts, safe
    for concurrent sensor publishes); unified `DISCOVERY_SCHEMA_VERSION`
    covering both sensor and bridge topics with a single migration key
    (`discovery_schema_version`) and a single migration pass per schema
    bump; bridge now uses the same device-based format as sensors
    (`homeassistant/device/ws2m_bridge_<mac>/config`).
  - `dongle_protocol.py` — renamed from `wyzesense.py`; fully
    snake_cased (`Open` → `open_dongle`, `Dongle.List` → `Dongle.list`,
    etc.); non-ASCII MAC bytes now decoded via latin-1 fallback instead
    of raising.
  - `bridge.py` — `Bridge` class orchestrating the above; no
    module-level globals.
  - `cli/bridge_tool.py` — replaces `bridge_tool_cli.py`; argparse
    replaces docopt; defaults to auto-detecting the dongle.
  - Old files (`wyzesense2mqtt.py`, `mqtt_common.py`, `wyzesense.py`,
    `bridge_tool_cli.py`, `wyzesense2mqtt_cli.py`) removed.
- Logging rationalised: sensor events and routine startup noise moved to
  `DEBUG`; all log messages carry a component name in the `name` field
  (`ws2m.bridge`, `ws2m.mqtt`, `ws2m.sensors`, `ws2m.dongle`).
- `requirements.txt` — removed `docopt` and `six`; added `pytest` for
  development.
- Docker base image pinned to `python:3.12-alpine`; `VOLUME /app/logs`
  removed.
- `self_topic_root` and `mqtt_client_id` defaults changed to `ws2m`
  (see Breaking changes above).

### Fixed

- Non-ASCII MAC bytes from the dongle no longer crash `SensorEvent`
  parsing; they are decoded via latin-1 with a warning log.
- Repeated "auto-added sensor" warnings suppressed after the first
  occurrence per session (until reload); avoids log spam for unconfigured
  sensors that report frequently.
- Fixed `clear_topics()` bug where `.add()` was called on a `list`
  (would have raised `AttributeError` on sensor removal for binary
  sensor types).

### Migration notes

Existing `config/config.yaml`, `config/sensors.yaml`, and
`config/state.yaml` files are fully compatible — no changes required.
New default keys are silently added on startup.

**Removed config keys** (`mqtt_qos`, `mqtt_retain`, `publish_sensor_name`)
are silently stripped from `config.yaml` on first 4.0 startup and will not
be written back.  `hass_topic_root` remains supported (previously this was
planned for removal but has been restored).

**`invert_state`** — if any sensors in `sensors.yaml` have
`invert_state: true`, this will now take effect automatically.  Previously
(3.1.0) this field existed in the file but was not applied in the bridge
logic.

**Per-sensor `timeout`** key in `sensors.yaml` is silently dropped on load
and will not be written back.  Availability timeouts are now type-driven.

`config/migrations.yaml` tracks the discovery schema version.  4.0.0
records `discovery_schema_version: 2` after the migration runs.  Installs
that already have version 2 recorded will not re-run the migration.

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


[Unreleased]: https://github.com/raetha/wyzesense2mqtt/compare/v3.0.2...HEAD
