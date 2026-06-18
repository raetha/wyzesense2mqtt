# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [4.0.0] — TBD

### Breaking changes

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

**Bridge HA device identity** — the bridge connection-state device
identifier changed from `wyzesense2mqtt_bridge_<mac>` to
`ws2m_bridge_<mac>` for consistency.  On first 4.0 startup the bridge
automatically clears the old retained discovery topic.  You will still
see a stale bridge device in HA's device registry; delete it manually
after upgrading — the new one will already be present.  Individual
sensor devices (`wyzesense_<mac>`) are unchanged.

**Python 3.12+ required** — 4.0 uses `X | Y` union type hint syntax
not available in earlier versions.  The Docker image now uses
`python:3.12-alpine`.

### Added

- **Test suite** (`tests/`) — 198 unit and integration tests covering
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
- **`__init__.py`** — package version (`__version__ = "4.0.0"`) as the
  single source of truth; imported by `mqtt.py` and `bridge.py`.
- **`log_level` config key** — controls verbosity without editing logging
  infrastructure.  Settable via `config.yaml` or `LOG_LEVEL` env var.
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

`config/migrations.yaml` tracks the discovery schema version.  Installs
with `discovery_schema_version: 2` already recorded will not re-run the
v1→v2 migration.

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
