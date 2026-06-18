#!/usr/bin/env python3
"""
WyzeSense2MQTT MQTT/discovery maintenance CLI.

A standalone tool for operating on the MQTT broker that wyzesense2mqtt
talks to.  It does not touch the USB dongle and does not require the bridge
service to be running.  For direct dongle management, see cli/bridge_tool.py.

Usage:
    python3 -m cli.maintenance cleanup-discovery [--apply] [--listen-seconds N]

Commands:
    cleanup-discovery   Scan for and optionally clear orphaned HA MQTT
                        discovery topics left behind by sensors that were
                        removed without going through the normal bridge flow.
                        Dry-run by default; pass --apply to clear topics.
"""

import argparse
import json
import logging
import os
import sys
import time

# Allow running from the wyzesense2mqtt/ directory directly
sys.path.insert(0, __file__.rsplit("/cli", 1)[0])

import paho.mqtt.client as mqtt
from config import SENSORS_CONFIG_FILE, config_path, load_config, read_yaml
from mqtt import _publish, clear_sensor_discovery_topics

LOGGER = logging.getLogger("ws2m.maintenance")

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
    """Return the sensor MAC if this topic looks like ours, otherwise None.

    A topic is ours if:
      - the device_id segment starts with 'wyzesense_' (but not 'wyzesense_bridge_')
      - at least one component unique_id starts with 'wyzesense_<mac>_'
    """
    if not device_id.startswith("wyzesense_") or device_id.startswith("wyzesense_bridge_"):
        return None

    mac = device_id.removeprefix("wyzesense_")

    # v2 device payload: unique_ids are inside components dict
    if "components" in payload:
        unique_ids = [c.get("unique_id", "") for c in payload.get("components", {}).values()]
    else:
        unique_ids = [payload.get("unique_id", "")]

    if not any(uid.startswith(f"wyzesense_{mac}_") for uid in unique_ids):
        return None

    return mac


# ---------------------------------------------------------------------------
# cleanup-discovery command
# ---------------------------------------------------------------------------


def run_cleanup_discovery(apply: bool = False, listen_seconds: int = 5) -> None:
    """Find (and optionally clear) orphaned HA MQTT discovery topics.

    Orphaned topics are those whose MAC is no longer present in sensors.yaml –
    i.e. sensors that were removed by hand rather than via the bridge's remove
    command, leaving retained discovery messages on the broker.
    """
    config, _ = load_config(LOGGER)
    if config is None:
        LOGGER.error("Could not load config – is config/config.yaml present and valid?")
        return

    if not config["hass_discovery"]:
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
        client_id=f"{config['mqtt_client_id']}_maintenance",
    )
    client.username_pw_set(username=config["mqtt_username"], password=config["mqtt_password"])
    client.on_message = _on_message
    client.connect(config["mqtt_host"], port=config["mqtt_port"], keepalive=config["mqtt_keepalive"])

    hass_root = config["hass_topic_root"]
    for wildcard in _DISCOVERY_WILDCARDS:
        adjusted = wildcard.replace("homeassistant", hass_root, 1)
        client.subscribe(adjusted)

    client.loop_start()
    LOGGER.info(f"Listening for retained discovery topics for {listen_seconds}s…")
    time.sleep(listen_seconds)

    # Load known sensors from disk
    sensors_path = config_path(SENSORS_CONFIG_FILE)
    sensors_config: dict = {}
    if os.path.isfile(sensors_path):
        sensors_config = read_yaml(sensors_path, LOGGER) or {}
    known_macs: set[str] = set(sensors_config.keys())

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

    print(f"Found {len(orphans)} orphaned discovery topic(s) not present in sensors.yaml:")
    for topic, mac, payload in orphans:
        device_name = payload.get("device", {}).get("name", "unknown")
        schema_ver = payload.get("schema_version", "v1 (legacy, untagged)")
        print(f"  {topic}")
        print(f"    mac={mac}  name={device_name!r}  schema_version={schema_ver}")

    if not apply:
        print("\nDry run – nothing cleared.  Re-run with --apply to remove these topics.")
        client.loop_stop()
        client.disconnect()
        return

    # Clear orphaned topics
    orphaned_macs: set[str] = {mac for _, mac, _ in orphans}

    for topic, _mac, _payload in orphans:
        LOGGER.info(f"Clearing: {topic}")
        _publish(client, config, LOGGER, topic, None, wait=False)

    for mac in orphaned_macs:
        # Also clear any sibling topics from other schema versions not
        # directly returned by the wildcard scan
        clear_sensor_discovery_topics(client, config, LOGGER, mac, "unknown", wait=False)
        _publish(client, config, LOGGER, f"{config['self_topic_root']}/{mac}/status", None, wait=False)
        _publish(client, config, LOGGER, f"{config['self_topic_root']}/{mac}", None, wait=False)

    time.sleep(1)  # allow publishes to flush
    client.loop_stop()
    client.disconnect()
    print(f"\nCleared {len(orphans)} orphaned discovery topic(s).")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WyzeSense2MQTT MQTT/discovery maintenance CLI",
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


if __name__ == "__main__":
    main()
