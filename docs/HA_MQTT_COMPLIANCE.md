# Home Assistant MQTT Discovery Compliance Notes

This file tracks the Home Assistant / MQTT integration version against which
the discovery payloads in `mqtt.py` have been reviewed and verified, so
future changes have a baseline to diff against.

## Last verified

- **Home Assistant version:** 2026.6.2
- **Reviewed:** 2026-06

## Discovery format in use

As of this review, sensor (and bridge) entities use the **device-based MQTT
discovery format** (one retained config topic per device, with all of that
device's entities listed under `components`):

```
homeassistant/device/wyzesense_<mac>/config
```

This replaces the older per-entity format
(`homeassistant/<component>/wyzesense_<mac>/<entity>/config`), which is still
supported by HA but is no longer the recommended approach. Device-based
discovery has been supported since **HA 2024.4**.

Each component entry includes:
- `platform` (e.g. `sensor`, `binary_sensor`) — required inside `components`
  since there's no longer a topic-derived component type.
- `unique_id`, `value_template`, `device_class`, etc.
- `has_entity_name: true` with `name: null` (or a short suffix like
  `"Extension probe"`) so entity names follow the modern
  `<Device Name> <Entity Name>` convention and pick up translated default
  names for `device_class`-based entities (e.g. "Battery", "Signal strength").

Device-level fields (`device`, `origin`, `availability`,
`availability_mode`, `state_topic`, `qos`) are set once at the top level and
shared by all components.

## Other changes made during this review

- Removed the deprecated top-level `"platform": "mqtt"` key from entity
  payloads (no longer used/needed by the MQTT integration's discovery
  schema).
- Added an `origin` block (`name`, `sw_version`, `support_url`) to sensor
  device discovery payloads, matching what was already done for the bridge.
- Added `suggested_display_precision` to numeric sensors (temperature,
  humidity, signal strength, battery) for cleaner default display in the UI.
- `clear_sensor_topics()` now clears discovery config topics from every known
  schema version (not just the current one) when a sensor is fully removed.
- Fixed a latent bug in the old `clear_topics()` where `entity_types.add(...)`
  was called on a `list` (should be `.append`/`.extend`) — this previously
  would have raised `AttributeError` for binary-sensor types when clearing
  topics.

## Discovery payload tagging & manual cleanup

Every device discovery payload now includes:

- `origin.name: "WyzeSense2MQTT"` — identifies the payload as ours.
- `schema_version: <DISCOVERY_SCHEMA_VERSION>` — the schema version it was
  published with.

HA ignores these as unrecognized top-level keys, but they're used by tooling
(see below) to find and identify our retained discovery topics on the
broker.

Additionally, the sensor data payload (published to
`<self_topic_root>/<mac>`) includes `wyzesense2mqtt_version` and
`discovery_schema_version`. Since several entities use
`json_attributes_topic` pointing at this topic, these show up as entity
attributes in HA — a quick visual check that a device is running the schema
you expect, and a hint that something's stale if a device's attributes show
an old `discovery_schema_version` after an upgrade.

### `cleanup-discovery` CLI

The versioned migration in `DongleWorker._init_sensors()` only handles sensors still
present in a dongle's `sensors.yaml`. If a sensor was removed by hand (edited out of
`sensors.yaml`/`state.yaml` after a failed unpair, rather than via the
`remove` MQTT topic), its discovery topic(s) can be orphaned indefinitely.

This is implemented in **`cli/maintenance.py`** (not the main bridge process,
which should be invoked via `__main__.py` or `service.sh`):

```
python3 -m cli.maintenance cleanup-discovery [--apply] [--listen-seconds N]
```

This subscribes to all known discovery wildcards — both the current
device-based format and the legacy per-entity format, since they live under
different topic paths:

- v2: `homeassistant/device/+/config`  (covers sensor, service, and dongle devices;
       the tool filters to `wyzesense_<mac>` device-ids only)
- v1: `homeassistant/sensor/+/+/config`, `homeassistant/binary_sensor/+/+/config`

then waits (`--listen-seconds`, default 5) for the broker to replay retained
messages (there's no "list retained topics" API — this is the same approach
a tool like MQTT Explorer uses under the hood). It then:

- Filters to payloads that look like ours: the device-id topic segment is
  `wyzesense_<mac>` (and not `wyzesense_bridge_...`), and a `unique_id` in
  the payload starts with `wyzesense_<mac>_`. (v1 payloads predate the
  `origin` tag, so identification relies on the topic/unique_id naming
  convention rather than `origin.name`.)
- Flags any whose MAC isn't in any dongle's `sensors.yaml`.
- Without `--apply`, just lists what it found (dry run).
- With `--apply`, clears the found topic(s) plus any other schema versions'
  topics and state/status topics for that MAC.

If retained messages don't arrive within `--listen-seconds`, increase it —
broker replay speed can vary.

### Discovery schema migration (automatic on upgrade)

Because retained MQTT messages don't disappear just because the code that
published them changed, switching discovery formats can otherwise leave
stale/duplicate entities in HA until the old retained configs are cleared.

To handle this (now and for any future discovery format change), there's a
small versioned migration system in `mqtt.py`:

- `DISCOVERY_SCHEMA_VERSION` (currently `2`) identifies the shape of
  discovery payloads this version of wyzesense2mqtt publishes.
- `_MIGRATION_STEPS` is a list of functions, one per schema version transition,
  each clearing the retained topics published by that version.
- `config/migrations.yaml` records the schema version last seen on disk.
- On startup, `DongleWorker._init_sensors()` compares the recorded version to
  `DISCOVERY_SCHEMA_VERSION`. If older, it runs every cleaner for the
  versions in between (once per known sensor), then updates
  `migrations.yaml`. This is a one-time pass per upgrade — already-migrated
  installs do nothing extra on subsequent restarts.
- `MqttGateway.clear_sensor_topics()` (used when a sensor is fully
  removed/unpaired) runs *every* cleaner regardless of recorded version, so
  a full removal never leaves stale entities behind.

### Reusing this for a future discovery format change

If a future HA/MQTT change means the topic structure needs to change again:

1. Add a `_clear_v3_discovery_topics(client, config, logger, mac, type, wait)`
   function in `mqtt.py` that clears whatever topics **v2** (the current
   format) published — i.e. the cleaner for version *N* always clears what
   version *N* itself published, so it can be run when migrating *away* from
   that version.
2. Add `3: _clear_v3_discovery_topics` to `_DISCOVERY_CLEANERS`.
3. Update `MqttGateway.publish_sensor_discovery()` to publish the new v3
   format.
4. Add a builder function to `_COMPONENT_BUILDERS` if the component structure
   changes.
5. Bump `DISCOVERY_SCHEMA_VERSION = 3`.
6. Update this doc's "Last verified" section.

Existing installs will then automatically clear their v2 (and, if still
pending, v1) topics on first startup with the new code, exactly as the v1→v2
migration does today.

## For future reviewers

When updating this again:
1. Check the current MQTT discovery schema at
   <https://www.home-assistant.io/integrations/mqtt/> (the "MQTT Discovery"
   section), specifically for changes to the `components`/device-based
   format, `origin`, and entity-naming (`has_entity_name`) conventions.
2. Update the "Last verified" version/date above.
3. Re-test discovery by deleting a sensor's retained config topic, restarting
   the bridge, and confirming entities appear correctly named under a single
   device in Settings → Devices & Services → MQTT.
