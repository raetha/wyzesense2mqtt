#!/usr/bin/env python3
"""
WyzeSense2MQTT MQTT/discovery maintenance CLI.

This is a standalone tool for operating on the MQTT broker
wyzesense2mqtt talks to - it does not touch the USB dongle and is not the
running bridge (that's wyzesense2mqtt.py, which should not be invoked
directly by users). For dongle pairing/management, see
bridge_tool_cli.py.

Usage:
    python3 wyzesense2mqtt_cli.py cleanup-discovery [--apply] [--listen-seconds N]
"""

import argparse
import json
import logging
import os
import time

import mqtt_common
import paho.mqtt.client as mqtt
from mqtt_common import CONFIG_PATH, SENSORS_CONFIG_FILE

LOGGER = logging.getLogger("wyzesense2mqtt_cli")


# --- cleanup-discovery -------------------------------------------------------
#
# Catches the case the version-based migration in wyzesense2mqtt.py doesn't:
# a sensor whose discovery topic(s) never got cleared (e.g. removed by
# editing sensors.yaml/state.yaml by hand after a failed unpair, rather than
# via the remove topic).
#
# There's no "list retained topics" broker API, so this subscribes to the
# discovery wildcards for every known schema version and waits for the
# broker to replay retained messages - the same approach a tool like MQTT
# Explorer uses under the hood. Only topics that look like ours (device-id
# topic segment matching wyzesense_<mac>, and a unique_id with the matching
# prefix) are considered, and only those whose MAC isn't in the current
# sensors.yaml are offered for cleanup.
#
# Subscribed wildcards, by schema version:
#   v2: homeassistant/device/+/config
#   v1: homeassistant/sensor/+/+/config
#       homeassistant/binary_sensor/+/+/config
_DISCOVERY_WILDCARDS = [
    "homeassistant/device/+/config",
    "homeassistant/sensor/+/+/config",
    "homeassistant/binary_sensor/+/+/config",
]


def _is_ours(topic, device_id, payload):
    if not device_id.startswith("wyzesense_") or device_id.startswith("wyzesense_bridge_"):
        return False
    mac = device_id.removeprefix("wyzesense_")
    unique_id = payload.get("unique_id", "")
    if "components" in payload:
        # v2 device-based payload: check a component's unique_id prefix
        unique_ids = [c.get("unique_id", "") for c in payload.get("components", {}).values()]
    else:
        unique_ids = [unique_id]
    if not any(uid.startswith(f"wyzesense_{mac}_") for uid in unique_ids):
        return False
    return mac


def run_discovery_cleanup(apply=False, listen_seconds=5):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config, _ = mqtt_common.load_config(LOGGER)

    if not config["hass_discovery"]:
        LOGGER.warning("hass_discovery is disabled in config; nothing to clean up.")
        return

    found = {}

    def on_message(client, userdata, msg):
        if msg.payload:
            try:
                found[msg.topic] = json.loads(msg.payload)
            except json.JSONDecodeError:
                LOGGER.debug(f"Ignoring non-JSON retained payload on {msg.topic}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{config['mqtt_client_id']}_cleanup")
    client.username_pw_set(username=config["mqtt_username"], password=config["mqtt_password"])
    client.on_message = on_message
    client.connect(config["mqtt_host"], port=config["mqtt_port"], keepalive=config["mqtt_keepalive"])

    for topic_filter in _DISCOVERY_WILDCARDS:
        client.subscribe(topic_filter.replace("homeassistant", config["hass_topic_root"], 1))

    client.loop_start()
    LOGGER.info(f"Listening for retained discovery topics for {listen_seconds}s...")
    time.sleep(listen_seconds)

    # Load currently-configured sensors directly from disk.
    sensors_config = {}
    if os.path.isfile(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)):
        sensors_config = mqtt_common.read_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), LOGGER) or {}
    known_macs = set(sensors_config.keys())

    orphans = []
    for topic, payload in found.items():
        parts = topic.split("/")
        if len(parts) < 4:
            continue
        device_id = parts[2]
        mac = _is_ours(topic, device_id, payload)
        if not mac:
            LOGGER.debug(f"Skipping {topic}: doesn't look like a wyzesense2mqtt discovery payload")
            continue
        if mac not in known_macs:
            orphans.append((topic, mac, payload))

    if not orphans:
        print("No orphaned wyzesense2mqtt discovery topics found.")
        client.loop_stop()
        client.disconnect()
        return

    print(f"Found {len(orphans)} orphaned discovery topic(s) not present in {SENSORS_CONFIG_FILE}:")
    for topic, mac, payload in orphans:
        device_name = payload.get("device", {}).get("name", "unknown")
        schema_version = payload.get("schema_version", "v1 (legacy, untagged)")
        print(f"  - {topic}")
        print(f"      mac={mac}  name='{device_name}'  schema_version={schema_version}")

    if not apply:
        print("\nDry run - nothing was cleared. Re-run with --apply to clear these retained topics.")
        client.loop_stop()
        client.disconnect()
        return

    orphaned_macs = {mac for _, mac, _ in orphans}
    for topic, _mac, _payload in orphans:
        LOGGER.info(f"Clearing orphaned discovery topic: {topic}")
        mqtt_common.mqtt_publish(client, config, LOGGER, topic, None, wait=False)

    for mac in orphaned_macs:
        # Clear topics from every known schema version for this mac (covers
        # any sibling topics not directly found above), plus state/status.
        mqtt_common.clear_all_discovery_topics(client, config, LOGGER, mac, "unknown", wait=False)
        mqtt_common.mqtt_publish(client, config, LOGGER, f"{config['self_topic_root']}/{mac}/status", None, wait=False)
        mqtt_common.mqtt_publish(client, config, LOGGER, f"{config['self_topic_root']}/{mac}", None, wait=False)

    # Give publishes a moment to flush before disconnecting
    time.sleep(1)
    client.loop_stop()
    client.disconnect()
    print(f"\nCleared {len(orphans)} orphaned discovery topic(s).")


def main():
    parser = argparse.ArgumentParser(description="WyzeSense2MQTT maintenance CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cleanup_parser = subparsers.add_parser(
        "cleanup-discovery",
        help="Scan for and optionally clear orphaned HA MQTT discovery topics (dry run by default)",
    )
    cleanup_parser.add_argument(
        "--apply", action="store_true", help="Actually clear the orphaned topics found (default is dry run)"
    )
    cleanup_parser.add_argument(
        "--listen-seconds",
        type=int,
        default=5,
        help="How long to wait for the broker to replay retained discovery topics (default: 5)",
    )

    args = parser.parse_args()

    if args.command == "cleanup-discovery":
        run_discovery_cleanup(apply=args.apply, listen_seconds=args.listen_seconds)


if __name__ == "__main__":
    main()
