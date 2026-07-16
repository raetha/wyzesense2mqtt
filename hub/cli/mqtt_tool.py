#!/usr/bin/env python3
"""
WyzeSense2MQTT MQTT tool – broker and discovery maintenance CLI.

A standalone tool for operating on the MQTT broker that wyzesense2mqtt
talks to.  It does not touch the USB dongle and does not require the bridge
service to be running.  For direct dongle management, see cli/dongle_tool.py.

Usage:
    python3 -m cli.mqtt_tool cleanup-discovery [--apply] [--listen-seconds N]
    python3 -m cli.mqtt_tool remove-dongle <mac> [--apply]

Commands:
    cleanup-discovery   Scan for and optionally clear orphaned HA MQTT
                        discovery topics left behind by sensors that were
                        removed without going through the normal bridge flow.
                        Dry-run by default; pass --apply to clear topics.

    remove-dongle       Remove a single dongle and all its sensors from
                        MQTT and local storage.  Shows a summary of what
                        will be cleared/deleted, then exits unless --apply
                        is passed.
"""

import argparse
import json
import logging
import os
import sys
import time

# Allow running from the wyzesense2mqtt/ directory directly
_pkg_root = __file__.rsplit("/cli", 1)[0]
sys.path.insert(0, _pkg_root)
# shared/ holds dongle_protocol and device_discovery in a git clone; Docker
# images and release packages flatten it into the app dir (no-op there).
import os as _os  # noqa: E402

_shared = _os.path.join(_os.path.dirname(_pkg_root), "shared")
if _os.path.isdir(_shared):
    sys.path.insert(0, _shared)

import paho.mqtt.client as mqtt  # noqa: E402
from config import (  # noqa: E402
    SENSOR_STATE_FILE,
    SENSORS_CONFIG_FILE,
    config_path,
    load_config,
    read_yaml,
    write_yaml,
)
from mqtt import (  # noqa: E402
    _publish,
    clear_dongle_topics,
    clear_sensor_discovery_topics,
    clear_sensor_state_topics,
)

LOGGER = logging.getLogger("ws2m.mqtt_tool")

# ---------------------------------------------------------------------------
# Discovery topic scanning
#
# MQTT brokers have no API for listing retained topics, so we subscribe to
# wildcard patterns and wait for the broker to replay retained messages to
# our client.  Wildcards covered (one per discovery schema version):
#   v2  homeassistant/device/+/config
#   v1  homeassistant/sensor/+/+/config
#       homeassistant/binary_sensor/+/+/config
# ---------------------------------------------------------------------------

_DISCOVERY_WILDCARDS = [
    "homeassistant/device/+/config",
    "homeassistant/sensor/+/+/config",
    "homeassistant/binary_sensor/+/+/config",
]


def _is_our_topic(topic: str, device_id: str, payload: dict) -> str | None:
    """Return the sensor MAC if this topic belongs to a wyzesense sensor, otherwise None.

    A topic is ours if:
      - the device_id segment starts with 'ws2m_sensor_' (v2 device topics) or
        'wyzesense_' (v1 per-entity topics, excluding 'wyzesense_bridge_')
      - at least one component unique_id starts with 'wyzesense_<mac>_'
    """
    if device_id.startswith("ws2m_sensor_"):
        mac = device_id.removeprefix("ws2m_sensor_")
    elif device_id.startswith("wyzesense_") and not device_id.startswith("wyzesense_bridge_"):
        mac = device_id.removeprefix("wyzesense_")
    else:
        return None

    # v2 device payload: unique_ids are inside components dict
    if "components" in payload:
        unique_ids = [c.get("unique_id", "") for c in payload.get("components", {}).values()]
    else:
        unique_ids = [payload.get("unique_id", "")]

    if not any(uid.startswith(f"wyzesense_{mac}_") for uid in unique_ids):
        return None

    return mac


def _schema_from_topic(topic: str, hass_root: str) -> str:
    """Derive the discovery schema version from the topic path.

    v2 topics live under <hass_topic_root>/device/…; v1 topics are the legacy
    per-entity <hass_topic_root>/<platform>/…/<entity>/config form.  The schema
    version is not carried in the payload — HA rejects unknown keys in device
    discovery payloads.
    """
    return "v2" if topic.startswith(f"{hass_root}/device/") else "v1 (legacy per-entity)"


# ---------------------------------------------------------------------------
# cleanup-discovery command
# ---------------------------------------------------------------------------


def run_cleanup_discovery(apply: bool = False, listen_seconds: int = 5) -> None:
    """Find (and optionally clear) orphaned HA MQTT discovery topics.

    Orphaned topics are those whose MAC is no longer present in any dongle's sensors.yaml –
    i.e. sensors that were removed by hand rather than via the bridge's remove
    command, leaving retained discovery messages on the broker.
    """
    cfg, _ = load_config(LOGGER)
    if cfg is None:
        LOGGER.error("Could not load config – is config/config.yaml present and valid?")
        return

    if not cfg["hass_discovery"]:
        LOGGER.warning("hass_discovery is disabled in config; nothing to clean up")
        return

    # Collect retained discovery topics by subscribing and listening
    found: dict[str, dict] = {}

    def _on_message(client, userdata, msg):
        if msg.payload:
            try:
                found[msg.topic] = json.loads(msg.payload)
            except json.JSONDecodeError:
                LOGGER.debug(f"Ignoring non-JSON retained payload on {msg.topic}")

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{cfg['mqtt_client_id']}_mqtt_tool",
    )
    client.username_pw_set(username=cfg["mqtt_username"], password=cfg["mqtt_password"])
    client.on_message = _on_message
    client.connect(cfg["mqtt_host"], port=cfg["mqtt_port"], keepalive=cfg["mqtt_keepalive"])

    hass_root = cfg["hass_topic_root"]
    for wildcard in _DISCOVERY_WILDCARDS:
        adjusted = wildcard.replace("homeassistant", hass_root, 1)
        client.subscribe(adjusted)

    client.loop_start()
    LOGGER.info(f"Listening for retained discovery topics for {listen_seconds}s…")
    time.sleep(listen_seconds)

    # Load known sensors from the flat registry file (<data>/sensors.yaml —
    # one file for the whole fleet; dongle ownership lives in state.yaml).
    known_macs: set[str] = set()
    sensors_path = config_path(SENSORS_CONFIG_FILE)
    if os.path.isfile(sensors_path):
        sensors_config = read_yaml(sensors_path, LOGGER) or {}
        known_macs.update(sensors_config.keys())

    # Identify orphans
    orphans: list[tuple[str, str, dict]] = []
    for topic, payload in found.items():
        parts = topic.split("/")
        if len(parts) < 4:
            continue
        device_id = parts[2]
        mac = _is_our_topic(topic, device_id, payload)
        if not mac:
            LOGGER.debug(f"Skipping {topic}: not a wyzesense2mqtt discovery topic")
            continue
        if mac not in known_macs:
            orphans.append((topic, mac, payload))

    # Report findings
    if not orphans:
        print("No orphaned wyzesense2mqtt discovery topics found.")
        client.loop_stop()
        client.disconnect()
        return

    print(f"Found {len(orphans)} orphaned discovery topic(s) not present in any dongle sensors.yaml:")
    for topic, mac, payload in orphans:
        device_name = payload.get("device", {}).get("name", "unknown")
        schema_ver = _schema_from_topic(topic, cfg["hass_topic_root"])
        print(f"  {topic}")
        print(f"    mac={mac}  name={device_name!r}  schema={schema_ver}")

    if not apply:
        print("\nDry run – nothing cleared.  Re-run with --apply to remove these topics.")
        client.loop_stop()
        client.disconnect()
        return

    # Clear orphaned topics
    orphaned_macs: set[str] = {mac for _, mac, _ in orphans}

    for topic, _mac, _payload in orphans:
        LOGGER.info(f"Clearing: {topic}")
        _publish(client, LOGGER, topic, None, wait=False)

    for mac in orphaned_macs:
        # Also clear any sibling topics from other schema versions not
        # directly returned by the wildcard scan
        clear_sensor_discovery_topics(client, cfg, LOGGER, mac, "unknown", wait=False)
        _publish(client, LOGGER, f"{cfg['self_topic_root']}/{mac}/status", None, wait=False)
        _publish(client, LOGGER, f"{cfg['self_topic_root']}/{mac}", None, wait=False)

    time.sleep(1)  # allow publishes to flush
    client.loop_stop()
    client.disconnect()
    print(f"\nCleared {len(orphans)} orphaned discovery topic(s).")


# ---------------------------------------------------------------------------
# remove-dongle command
# ---------------------------------------------------------------------------


def run_remove_dongle(dongle_mac: str, apply: bool = False) -> None:
    """Remove a single dongle and all its sensors from MQTT and local storage.

    Connects to the MQTT broker, clears all retained discovery and status
    topics for the dongle and every sensor it owns, then (if ``--apply`` is
    passed) removes those sensors from the registry files (sensors.yaml and
    state.yaml).  A dongle's sensors are the state entries whose ``dongle``
    key names it.

    Dry-run by default; pass ``--apply`` to make changes.
    """
    cfg, _ = load_config(LOGGER)
    if cfg is None:
        LOGGER.error("Could not load config – is config/config.yaml present and valid?")
        return

    sensors_config: dict = read_yaml(config_path(SENSORS_CONFIG_FILE), LOGGER) or {}
    state: dict = read_yaml(config_path(SENSOR_STATE_FILE), LOGGER) or {}
    known_macs = sorted({e["dongle"] for e in state.values() if isinstance(e, dict) and e.get("dongle")})
    if dongle_mac not in known_macs:
        LOGGER.error(f"Dongle MAC {dongle_mac!r} not found in state.yaml. Known MACs: {known_macs or ['(none)']}")
        return

    sensor_macs = [mac for mac, entry in state.items() if isinstance(entry, dict) and entry.get("dongle") == dongle_mac]

    print(f"Dongle: {dongle_mac}")
    print(f"Sensors ({len(sensor_macs)}):")
    for mac in sensor_macs:
        s = sensors_config.get(mac, {})
        print(f"  {mac}  type={s.get('sensor_type', 'unknown')}  name={s.get('name', '')!r}")

    print(f"\nActions that {'WILL' if apply else 'WOULD'} be taken:")
    print(f"  • Clear retained MQTT discovery topic for dongle {dongle_mac}")
    for mac in sensor_macs:
        print(f"  • Clear retained MQTT topics for sensor {mac}")
    print(f"  • Remove {len(sensor_macs)} sensor(s) from sensors.yaml and state.yaml")

    if not apply:
        print("\nDry run — nothing changed.  Re-run with --apply to make these changes.")
        return

    # Connect to MQTT
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{cfg['mqtt_client_id']}_mqtt_tool",
    )
    client.username_pw_set(username=cfg["mqtt_username"], password=cfg["mqtt_password"])
    client.connect(cfg["mqtt_host"], port=cfg["mqtt_port"], keepalive=cfg["mqtt_keepalive"])
    client.loop_start()

    # Clear sensor topics
    for mac in sensor_macs:
        sensor_type = sensors_config.get(mac, {}).get("sensor_type", "unknown")
        LOGGER.info(f"Clearing sensor topics: {mac}")
        clear_sensor_state_topics(client, cfg, LOGGER, mac, sensor_type, wait=False)
        clear_sensor_discovery_topics(client, cfg, LOGGER, mac, sensor_type, wait=False)

    # Clear dongle topics
    LOGGER.info(f"Clearing dongle topics: {dongle_mac}")
    clear_dongle_topics(client, cfg, LOGGER, dongle_mac, wait=False)

    time.sleep(1)
    client.loop_stop()
    client.disconnect()

    # Remove the dongle's sensors from the registry files
    for mac in sensor_macs:
        sensors_config.pop(mac, None)
        state.pop(mac, None)
    write_yaml(config_path(SENSORS_CONFIG_FILE), sensors_config, LOGGER)
    write_yaml(config_path(SENSOR_STATE_FILE), state, LOGGER)
    print(f"\nRemoved {len(sensor_macs)} sensor(s) from the registry files.")

    print(f"\nDone.  Dongle {dongle_mac} removed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WyzeSense2MQTT MQTT tool – broker and discovery maintenance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cleanup_p = sub.add_parser(
        "cleanup-discovery",
        help="Scan for and optionally clear orphaned HA MQTT discovery topics",
    )
    cleanup_p.add_argument(
        "--apply",
        action="store_true",
        help="Actually clear orphaned topics (default is dry run)",
    )
    cleanup_p.add_argument(
        "--listen-seconds",
        type=int,
        default=5,
        metavar="N",
        help="Seconds to wait for the broker to replay retained topics (default: 5)",
    )

    remove_p = sub.add_parser(
        "remove-dongle",
        help="Remove a single dongle and all its sensors from MQTT and local storage",
    )
    remove_p.add_argument(
        "mac",
        help="MAC address of the dongle to remove (as recorded in state.yaml)",
    )
    remove_p.add_argument(
        "--apply",
        action="store_true",
        help="Actually clear MQTT topics and delete data files (default is dry run)",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "cleanup-discovery":
        run_cleanup_discovery(apply=args.apply, listen_seconds=args.listen_seconds)
    elif args.command == "remove-dongle":
        run_remove_dongle(args.mac, apply=args.apply)


if __name__ == "__main__":
    main()
