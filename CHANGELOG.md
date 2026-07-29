# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [4.0.1] — 2026-07-29

Hardening for the remote bridge feature (the connection between a hub and a
remote dongle relay) introduced in 4.0.0, plus two chime fixes. Nothing here
requires any action on your part — everything takes effect automatically on
upgrade, including for remotes and chimes you already have set up.

### Security

- **A remote's name is now checked before it's used.** Each remote relay has
  a name (`remote_id`) chosen during setup, which the hub used, unchecked,
  when saving that remote's credential file and building its MQTT topics.
  A deliberately crafted name could have caused the hub to write outside its
  intended data folder, or interfered with other MQTT topics. Names are now
  restricted to a safe set of characters, and anything else is rejected.
- **Fixed a theoretical timing weakness** in how the hub checks a remote's
  saved credential.
- **A misbehaving or malicious connection can no longer make the hub hang.**
  A remote reconnecting after a network hiccup can ask the hub to replay a
  backlog of missed events; that backlog size is now capped to a sane limit
  instead of being trusted blindly.
- **Credential files are now created readable only by the account running
  ws2m**, rather than inheriting more permissive default file permissions.
  Existing credential files (from anyone already on 4.x) are corrected
  automatically the next time the hub or remote starts — nothing to do.
- Re-adopting a remote that lost its saved credential (for example, after
  wiping its data volume) still works automatically, but the hub now logs a
  clear warning when this happens so it doesn't go unnoticed.

### Fixed

- **The chime's "Play sound" button wasn't showing up in Home Assistant.**
  An invalid setting in its device configuration caused Home Assistant to
  reject the whole device, so the button and its other controls never
  appeared. (#107)
- **The chime no longer shows a battery.** It's a plug-in accessory with no
  battery, but was incorrectly getting battery entities in Home Assistant.
  Removed; signal strength and chip temperature are unaffected.

## [4.0.0] — 2026-07-16


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

**Scan and remove MQTT topics are now dongle-scoped** — `ws2m/scan` and
`ws2m/remove` are now `ws2m/dongle/<mac>/scan` and `ws2m/dongle/<mac>/remove`.

**Shared code moved to `shared/`** — `dongle_protocol.py` and the new
`device_discovery.py` are shared by the hub and the remote. Docker images and
release packages flatten `shared/` alongside the app code, so nothing changes
for Docker users; git-clone native installs must keep `shared/` next to
`hub/`/`remote/` (the entry points pick it up automatically).

**Python 3.12+ required.**

### Added

- **Sensor fleet visibility** — the hub device gains **Paired sensors**
  (distinct sensors reported paired by currently-connected dongles) and
  **Configured sensors** (registry entries) diagnostic entities, plus a
  **Cleanup orphaned sensors** button that removes registry entries and
  retained topics for sensors no longer paired to any connected dongle. A gap
  between the two counts is the signal that something stale exists.
- **Staged orphan lifecycle** — an hourly sweep marks sensors that are no
  longer paired to any connected dongle (their entities simply show
  unavailable; config is untouched, and everything reactivates automatically
  if the sensor, dongle, or remote returns). Sensors orphaned continuously
  for longer than `orphan_retention_days` (default 7; 0 disables) have their
  retained topics cleared and registry entries removed automatically. The
  retention window is adjustable live via the **Orphan retention** number
  entity on the hub device (or `orphan_retention_days` in config).
- **Remote bridge** — a new `ws2m-remote` image runs on any machine with one
  or more USB WyzeSense dongles and forwards raw HID frames to the hub over
  authenticated WebSockets (one connection per dongle, so dongles are fully
  independent of each other). The hub runs the full sensor protocol; the
  remote is fully transparent. Each dongle supports hot-reconnect with its own
  replay buffer (10 s TTL, 500-frame ring buffer) so brief network disruptions
  do not drop sensor events.
  Remotes auto-discover the hub via mDNS (`_ws2m._tcp.local.`) when
  `WS2M_HUB_URL` is not set — useful for simple single-hub setups. Set
  `WS2M_HUB_URL` explicitly when crossing VLANs or in Docker without host
  networking. Enable remote connections on the hub with `hub_ws_enabled: true`
  in `config.yaml` or `WS2M_HUB_WS_ENABLED=true` in the environment; the HA
  hub device also exposes a **WebSocket remote listener** switch for toggling
  this at runtime. Activate pairing mode via the `ws2m/hub/<uuid>/remote_pair` button
  in HA to adopt new remotes — no pre-shared secret is required. Each remote
  appears in HA with `health` (healthy/degraded, aggregated across its
  dongles), `connected_dongles` (count of relayed dongles), a `Restart`
  button, a dongle selector and log level (both persisted on the remote and
  applied at startup; `WS2M_DONGLE`/`WS2M_LOG_LEVEL` env vars pin them), and a
  **Remove remote** button that clears all MQTT topics for the remote and its
  entire dongle and sensor chain.
  See `examples/hub/` and `examples/remote/` for compose and service file examples.
- **Multi-dongle support** — `dongle: auto` (the default) connects to all
  WyzeSense bridge dongles at startup, on the hub and on remotes alike. Each
  gets its own worker and dongle-scoped MQTT topics. Explicit paths (`/dev/hidrawN`)
  remain supported, and `dongle` may also be set to a directory (e.g.
  `/dev/ws2m-dongles`) to use every device node inside it — pairs with the
  example udev rule in `examples/99-ws2m-dongles.rules.example`, which collects
  all WyzeSense dongles into one folder that can be bind mounted into a
  container (see the compose examples for the matching `device_cgroup_rules`).
  Auto-detection prefers the canonical `/dev/hidrawN` node and returns exactly
  one path per physical dongle, so a dongle reachable via both its canonical
  node and a udev alias is never opened twice.
- **HA device hierarchy** — three-level device tree: `ws2m_hub_<uuid>` →
  `ws2m_dongle_<mac>` → `wyzesense_<mac>`, linked via `via_device` so HA shows
  the full chain. A stable hub UUID is generated on first run and persisted
  to `hub.yaml`.
- **Wyze Sense Keypad v2 (WSKP1)** — publishes arm/disarm mode, motion, and PIN
  events to MQTT. Creates an `alarm_control_panel` and `motion` binary sensor in
  HA. Supports PIN validation and pushes state back to the keypad display/LEDs
  via `CMD_SEND_KEYPAD_EVENT`. PIN codes are stored only in `sensors.yaml` and
  are never published to MQTT. See [docs/keypad.md](docs/keypad.md).
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
- **Docker `HEALTHCHECK`** — both images write and periodically touch
  `/tmp/ws2m_healthy` while running. Health is strictly scoped to each
  container's own area: the hub flips unhealthy within ~90 s of a process hang
  or when every *local* dongle has failed (remote problems never degrade the
  hub — they surface on the remote and dongle devices in HA); the remote flips
  unhealthy when every dongle it relays is lost.
- **Test suite** — 590 unit, integration, and hardware tests covering all modules.
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

- **Global sensor registry** — `<data>/sensors.yaml` and `<data>/state.yaml`
  hold the whole sensor fleet across every dongle (local and remote); 3.1.0
  files load as-is. Sensor configuration (name, class, invert_state, PINs,
  chime settings) is a property of the sensor and follows it if it is
  re-paired to a different dongle; the owning dongle is recorded per entry in
  `state.yaml` and transfers automatically when events arrive via a different
  dongle (HA `via_device` updates on the fly). When ownership transfers and
  the old dongle is still connected, the stale pairing is deleted from the old
  dongle's NVRAM so only the active link exists anywhere. All registry writes
  are atomic and serialised, so concurrent updates from multiple dongles can
  never corrupt the files. Sensor state is never discarded for age —
  `last_seen` values are absolute timestamps, so the per-sensor-type
  availability timeouts (4 h / 8 h / 24 h) handle any length of downtime
  naturally.
- **Major package refactor** — `wyzesense2mqtt.py` (881-line monolith) replaced
  by a structured package: `config.py`, `sensors.py`, `mqtt.py`,
  `dongle_protocol.py` (renamed from `wyzesense.py`, fully snake_cased),
  `bridge.py`, and `cli/`. Old files removed.
- **MQTT topic restructure** — sensor topics moved to `ws2m/sensor/<mac>/`;
  dongle topics to `ws2m/dongle/<mac>/`; hub service topics to
  `ws2m/hub/<uuid>/` (UUID included for multi-hub support). Old flat-root
  sensor and dongle topics are cleared as retained on first 4.0 start.
- **HA MQTT discovery** upgraded to device-based format
  (`homeassistant/device/ws2m_sensor_<mac>/config` with `components`), supported
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
  attached sensors offline, and saves state. Remaining healthy workers
  continue unaffected; the container is marked unhealthy only if every local
  dongle has failed.
- **`invert_state` re-implemented** — present in `sensors.yaml` since v1.1 but
  dropped from bridge logic in v3.1.0. Now applied correctly: swaps
  `payload_on`/`payload_off` in HA discovery for contact and motion sensors.
- Non-ASCII MAC bytes no longer crash event parsing; decoded via latin-1 with a
  warning.
### Migration notes

Existing 3.1.0 `config.yaml`, `sensors.yaml`, and `state.yaml` files are
compatible — no manual changes required; sensors are claimed by their dongle
on first start. Removed config keys (`mqtt_qos`, `mqtt_retain`,
`publish_sensor_name`, per-sensor `timeout`) are silently stripped on first
load. `migrations.yaml` records `discovery_schema_version: 2` after the v1→v2
migration runs; subsequent starts skip the migration. Interim 4.0.0-devel
per-dongle `dongles/<mac>/` directories are merged into the flat registry
files automatically (originals kept at `dongles.migrated/`).

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


[4.0.1]: https://github.com/raetha/wyzesense2mqtt/compare/v4.0.0...v4.0.1
[4.0.0]: https://github.com/raetha/wyzesense2mqtt/compare/v3.1.0...v4.0.0
[3.1.0]: https://github.com/raetha/wyzesense2mqtt/compare/v3.0.2...v3.1.0
