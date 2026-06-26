# CLI Tools

ws2m includes two command-line tools for situations where the normal service
workflow — pairing, removing sensors, reloading config — is not available or
not sufficient. Most users will never need either of these.

> **Stop the bridge before using `dongle_tool`.** The bridge holds the HID
> device open exclusively; a second process cannot open it at the same time.
> If running as a Docker container: `docker stop wyzesense2mqtt`. If running
> under systemd: `sudo systemctl stop wyzesense2mqtt`.
>
> `mqtt_tool` does not touch the USB dongle and can be run while the bridge
> is running.

### Running `dongle_tool` from Docker

Docker users can run `dongle_tool` directly from the same image without
installing anything separately. Stop the bridge first, then start a throwaway
container with the dongle passed through and the entrypoint overridden:

```bash
docker stop wyzesense2mqtt

docker run --rm -it \
  --device /dev/hidraw0:/dev/hidraw0 \
  --volume /docker/wyzesense2mqtt/data:/app/data \
  --entrypoint sh \
  ghcr.io/raetha/wyzesense2mqtt:latest \
  -c "python3 -m cli.dongle_tool --device /dev/hidraw0 list"
```

Replace `/dev/hidraw0` with your actual device path and
`/docker/wyzesense2mqtt/data` with your `VOL_DATA` path. Swap `list` for any
other `dongle_tool` command. The `--volume` mount is only needed for commands
that read `sensors.yaml` (e.g. `list` — to show sensor names alongside MACs).

When you are done, restart the bridge:

```bash
docker start wyzesense2mqtt
```

---

## `cli/dongle_tool.py` — Direct USB dongle access

Provides direct access to the WyzeSense USB dongle without the bridge service
or an MQTT broker. Useful for:

- Pairing or unpairing sensors on a dongle without using the Wyze app or
  running the full bridge service
- Diagnosing a dongle that the bridge cannot open or communicate with
- Removing corrupt/null sensor MACs that accumulate after battery failures
- Monitoring raw sensor events for debugging

**Important:** `dongle_tool` operates only at the USB/firmware level. Pairing
a sensor here registers it with the dongle hardware — the `sensors.yaml` entry
is created automatically the first time the bridge receives an event from that
sensor. Unpairing does not update `sensors.yaml`; use `mqtt_tool remove-dongle`
or the **Cleanup removed dongles** button in HA for a clean removal that also
clears MQTT state and config.

Run from inside the container (`docker exec -it wyzesense2mqtt sh`) or directly
on the host:

```bash
# List all sensors paired to the dongle
python3 -m cli.dongle_tool --device /dev/hidraw0 list

# Pair a new sensor (waits up to 60 s for a sensor in pairing mode)
python3 -m cli.dongle_tool --device /dev/hidraw0 pair

# Unpair a sensor by MAC
python3 -m cli.dongle_tool --device /dev/hidraw0 unpair AABBCCDD

# Remove sensors with corrupt/null MACs (common after battery failure)
python3 -m cli.dongle_tool --device /dev/hidraw0 fix

# Monitor live sensor events (useful for confirming a sensor is reachable)
python3 -m cli.dongle_tool --device /dev/hidraw0 monitor

# Show all available commands and options
python3 -m cli.dongle_tool --help
```

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
storage. Prints the dongle MAC, all sensors with their type and name, and the
data directory path before making any changes.

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
