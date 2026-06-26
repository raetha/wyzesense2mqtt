# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [4.0.0] — TBD

### Breaking changes

**Data directory renamed** — the default data directory inside the container
has been renamed from `config/` to `data/` (`/app/config` → `/app/data`).
On first start, `service.sh` creates a symlink so existing mounts continue
to work. To migrate cleanly, update your volume mount to `/app/data` and
remove the symlink.

**Environment variable prefix** — all environment variables now require a
`WS2M_` prefix (e.g. `MQTT_HOST` → `WS2M_MQTT_HOST`). Unprefixed names are
still accepted in 4.0 but are deprecated. Update your `.env` files and
Docker Compose blocks — see `examples/`.

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

**Bridge HA device identity** — the single `ws2m_bridge_<mac>` device is now
two devices: `ws2m_service_<uuid>` (software) and `ws2m_dongle_<mac>`
(hardware). On first 4.0 startup the bridge clears all pre-4.0 retained
topics automatically (schema v1→v2). Delete the stale bridge device from HA's
device registry manually after upgrading.

**Sensor data directory restructured** — sensor files have moved from
`<data>/sensors.yaml` and `<data>/state.yaml` to
`<data>/dongles/<dongle_mac>/sensors.yaml` and `state.yaml`. Existing flat
files are migrated automatically on first start.

**Scan and remove MQTT topics are now dongle-scoped** — `ws2m/scan` and
`ws2m/remove` are now `ws2m/dongle_<mac>/scan` and `ws2m/dongle_<mac>/remove`.

**Python 3.12+ required.**

### Added

- **Multi-dongle support** — `usb_dongle: auto` (the default) connects to all
  WyzeSense bridge dongles at startup. Each gets its own worker with independent
  sensor registry and dongle-scoped MQTT topics. Explicit paths (`/dev/hidrawN`)
  remain supported.
- **HA device hierarchy** — three-level device tree: `ws2m_service_<uuid>` →
  `ws2m_dongle_<mac>` → `wyzesense_<mac>`, linked via `via_device` so HA shows
  the full chain. A stable service UUID is generated on first run and persisted
  to `service.yaml`.
- **Wyze Sense Keypad v2 (WSKP1)** — publishes arm/disarm mode, motion, and PIN
  events to MQTT. Creates an `alarm_control_panel` and `motion` binary sensor in
  HA. Supports PIN validation and pushes state back to the keypad display/LEDs
  via `CMD_SEND_KEYPAD_EVENT`. See [docs/keypad.md](docs/keypad.md).
- **Wyze Video Doorbell V1 Chime (WCHIME1)** — play button and number entities
  for ring tone (0–255), volume (1–9), and repeat count (1–9). Values persist to
  `sensors.yaml`. Ring tone IDs are undocumented; see
  [docs/contributing_protocol.md](docs/contributing_protocol.md).
- **HA configuration entities** — sensor settings adjustable live from the HA
  device page: sensor name (text), device class (select), invert state (switch),
  and log level (select, service device). Keypad adds arm PIN capture and clear
  PINs buttons plus a PIN count sensor. Cleanup removed dongles button on the
  service device removes MQTT topics and data for dongles no longer connected.
- **Home Assistant App** — available via
  [raetha/home-assistant-apps](https://github.com/raetha/home-assistant-apps).
  `service.sh` detects `/data/options.json` and loads config automatically,
  including Mosquitto broker auto-discovery via the Supervisor API.
- **Docker `HEALTHCHECK`** — the bridge writes and periodically touches
  `/tmp/ws2m_healthy` while running; removes it on failure. Container flips
  unhealthy within ~90 s of a dongle failure or process hang.
- **Test suite** — 356 unit and integration tests covering all modules.
  Hardware smoke tests behind `pytest -m dongle`. Run with
  `bash scripts/run_tests.sh`.
- **`cli/mqtt_tool.py`** — MQTT maintenance CLI: `cleanup-discovery` finds
  orphaned HA discovery topics; `remove-dongle <mac>` decommissions a dongle
  and clears all its retained topics and data.
- **`docs/contributing_protocol.md`** and **`tools/fuzz_keypad.py`** — HID
  capture guide and systematic protocol fuzzer for contributors.

### Changed

- **Major package refactor** — `wyzesense2mqtt.py` (881-line monolith) replaced
  by a structured package: `config.py`, `sensors.py`, `mqtt.py`,
  `dongle_protocol.py` (renamed from `wyzesense.py`, fully snake_cased),
  `bridge.py`, and `cli/`. Old files removed.
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
- `examples/` files are the single source of truth for Docker Compose and `.env`
  configuration.

### Fixed

- **USB dongle disconnect** — an `OSError` from the HID read loop was previously
  swallowed silently, leaving the worker spinning indefinitely with no output.
  The error now propagates: the bridge logs it, publishes the dongle and all
  attached sensors offline, saves state, and marks the container unhealthy.
  Remaining healthy workers continue unaffected.
- **`invert_state` re-implemented** — present in `sensors.yaml` since v1.1 but
  dropped from bridge logic in v3.1.0. Now applied correctly: swaps
  `payload_on`/`payload_off` in HA discovery for contact and motion sensors.
- Sensor name shown in online/offline log messages: `AABBCCDD (Front Door) is
  back online`.
- Scan `TimeoutError` now logged as a warning instead of swallowed silently.
- Non-ASCII MAC bytes no longer crash event parsing; decoded via latin-1 with a
  warning.
- Repeated "auto-added sensor" warnings suppressed after first occurrence per
  session.
- `clear_topics()` `AttributeError` on sensor removal fixed.

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


[Unreleased]: https://github.com/raetha/wyzesense2mqtt/compare/v3.0.2...HEAD
