# CLI Tools

ws2m includes two command-line tools for situations where the normal service
workflow — pairing, removing sensors, reloading config — is not available or
not sufficient. Most users will never need either of these.

> **Stop the hub before using `dongle_tool`.** The hub holds the HID device
> open exclusively; a second process cannot open it at the same time. With
> Docker Compose (the example's container is `ws2m-hub`): `docker stop ws2m-hub`.
> Under systemd: `sudo systemctl stop ws2m-hub`.
>
> `mqtt_tool` does not touch the USB dongle and can be run while the hub is
> running.

### Running the tools from Docker

The `ws2m-hub` image includes both tools; its working directory is `/app`, where
they live. For `dongle_tool`, stop the hub, then start a throwaway container
with the dongle passed through:

```bash
docker stop ws2m-hub

docker run --rm -it \
  --device /dev/hidraw0:/dev/hidraw0 \
  --entrypoint python3 \
  ghcr.io/raetha/ws2m-hub:latest \
  -m cli.dongle_tool --device /dev/hidraw0 list

docker start ws2m-hub
```

Replace `/dev/hidraw0` with your dongle's device path, and `list` with any
other command below.

`mqtt_tool` runs inside the running hub container, which already has your
MQTT settings:

```bash
docker exec -it ws2m-hub python3 -m cli.mqtt_tool cleanup-discovery
```

Without Docker, run them from the `hub/` folder of a clone or release package
(`cd hub && python3 -m cli.dongle_tool …`).

The tools aren't available inside the Home Assistant app; use a Docker host or
a clone for them.

---

## `cli/dongle_tool.py` — Direct USB dongle access

Provides direct access to the WyzeSense USB dongle without the hub service or
an MQTT broker. Useful for:

- Pairing or unpairing sensors on a dongle without running the hub
- Diagnosing a dongle that the hub cannot open or communicate with
- Removing corrupt/null sensor MACs that accumulate after battery failures
- Clearing a dongle whose pairing table is unreadable (see below)
- Monitoring raw sensor events for debugging

**Important:** `dongle_tool` operates only at the USB/firmware level. Pairing
a sensor here registers it with the dongle hardware — the `sensors.yaml` entry
is created automatically the first time the hub receives an event from that
sensor. Unpairing does not update `sensors.yaml`; sensors that stay unpaired
are cleaned up by the hub's orphan handling, or right away with the
**Cleanup orphaned sensors** button in HA.

```bash
# List all sensors paired to the dongle
python3 -m cli.dongle_tool --device /dev/hidraw0 list

# Pair a new sensor (waits up to 60 s for a sensor in pairing mode)
python3 -m cli.dongle_tool --device /dev/hidraw0 pair

# Unpair a sensor by MAC (says so if it wasn't paired)
python3 -m cli.dongle_tool --device /dev/hidraw0 unpair AABBCCDD

# Remove corrupt/null sensor MACs (fetches paired list, identifies invalids, removes only those)
python3 -m cli.dongle_tool --device /dev/hidraw0 fix

# Show what's paired, then unpair every sensor, corrupt slots included
python3 -m cli.dongle_tool --device /dev/hidraw0 reset
python3 -m cli.dongle_tool --device /dev/hidraw0 reset --yes

# Monitor live sensor events (useful for confirming a sensor is reachable)
python3 -m cli.dongle_tool --device /dev/hidraw0 monitor

# Show all available commands and options
python3 -m cli.dongle_tool --help
```

### A pairing slot that can't be read

An old dongle can hold a corrupt pairing slot: the dongle counts it, but never
sends its record when asked for the list. `list` then shows the sensors it
could read and says how many never arrived, and the hub logs
`Timeout fetching sensor list (received 3/4)`. The hub keeps working with the
sensors it could read.

`unpair` can't remove such a slot, because its MAC is unknown, and `fix` only
removes entries it can see. `reset --yes` sends the dongle's "delete all
sensors" command, which clears every slot including the corrupt one. Note the
sensors `reset` lists first; each has to be paired again afterwards (`pair`,
or **Scan for sensor** on the dongle's device page in Home Assistant). Their
names and settings in `sensors.yaml` apply again if they're paired within
`orphan_retention_days` (7 by default).

---

## `cli/mqtt_tool.py` — MQTT broker maintenance

Operates on the MQTT broker directly. Does not touch the USB dongle. Useful for:

- Cleaning up orphaned HA discovery topics left behind after sensors were
  removed by editing `sensors.yaml` directly rather than via the MQTT command
- Surgically decommissioning a single dongle (clearing its MQTT topics and
  deleting its data directory) without affecting other dongles

All destructive operations require `--apply` to be passed explicitly; the
default is always a dry run that prints a full summary of what would change.

Run from inside the container or on any host that can reach the MQTT broker:

### `cleanup-discovery`

Scans for HA discovery topics belonging to sensors no longer present in any
dongle's `sensors.yaml` and reports them. Pass `--apply` to clear the
orphaned retained topics.

```bash
# Dry run — show orphaned topics without making changes
python3 -m cli.mqtt_tool cleanup-discovery

# Clear orphaned topics
python3 -m cli.mqtt_tool cleanup-discovery --apply

# Increase listen time if the broker is slow to replay retained messages
python3 -m cli.mqtt_tool cleanup-discovery --listen-seconds 15
```

### `remove-dongle`

Permanently removes a single dongle and all of its sensors from MQTT and local
storage. A dongle's sensors are the `state.yaml` entries that record it as
their owner. Prints the dongle MAC and all sensors with their type and name
before making any changes; with `--apply` those sensors are also removed from
`sensors.yaml` and `state.yaml`.

```bash
# Dry run — show what would be removed
python3 -m cli.mqtt_tool remove-dongle AABBCCDD

# Clear MQTT topics and delete the data directory
python3 -m cli.mqtt_tool remove-dongle AABBCCDD --apply
```

> **Note:** For removing a dongle from a running bridge, the **Cleanup removed
> dongles** button on the HA service device page is the preferred method —
> it handles the same cleanup without needing shell access. Use `remove-dongle`
> when you need to target a specific dongle, or when the bridge is not running.
