#!/usr/bin/env python3
"""
WyzeSense bridge tool – direct USB dongle CLI.

Provides command-line access to the WyzeSense USB dongle for pairing,
unpairing, listing sensors, and low-level diagnostics.  This tool
communicates directly with the dongle hardware and does **not** require
the bridge service or an MQTT broker to be running.

For MQTT/discovery maintenance, see cli/maintenance.py instead.

Usage:
    python3 -m cli.bridge_tool [--device PATH] [--debug] [--verbose]

Options:
    --device PATH   Path to the USB HID device, or 'auto' to detect automatically [default: auto]
    --debug         Enable debug logging
    --verbose       Increase log verbosity (combined with --debug)
"""

import argparse
import binascii
import logging
import os
import sys

# Allow running from the wyzesense2mqtt/ directory directly
sys.path.insert(0, __file__.rsplit("/cli", 1)[0])

import dongle_protocol as dp

# ---------------------------------------------------------------------------
# Event callback
# ---------------------------------------------------------------------------


def _on_event(dongle: dp.Dongle, event) -> None:
    """Print sensor events to stdout as they arrive."""
    if isinstance(event, str):
        print(f"[dongle] {event}")
        return
    from datetime import datetime

    ts = datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"[{ts}][{event.mac}] type={event.event}"]
    for attr in ("sensor_type", "state", "battery", "signal_strength"):
        if hasattr(event, attr):
            parts.append(f"{attr}={getattr(event, attr)}")
    print(", ".join(parts))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(dongle: dp.Dongle, _args) -> None:
    """List all sensors currently paired with the dongle."""
    try:
        sensors = dongle.list()
    except TimeoutError:
        print("Error: timed out while retrieving sensor list")
        return

    print(f"{len(sensors)} sensor(s) paired:")
    for mac in sensors:
        try:
            mac.encode("ascii")
            display = mac
        except UnicodeEncodeError:
            display = "".join(f"{ord(c):02x}" for c in mac)
        print(f"  {display}")


def cmd_pair(dongle: dp.Dongle, _args) -> None:
    """Scan for a new sensor and pair it (waits up to 60 s)."""
    print("Waiting for a sensor in pairing mode (up to 60 s)…")
    result = dongle.scan()
    if result:
        mac, sensor_type, sensor_version = result
        print(f"Paired: mac={mac}  type={sensor_type}  version={sensor_version}")
    else:
        print("No sensor found")


def cmd_unpair(dongle: dp.Dongle, args) -> None:
    """Unpair one or more sensors by MAC address.

    Accepts 8-character ASCII MACs or 16-character hex-encoded MACs
    (as displayed by 'list' for sensors with non-ASCII addresses).
    """
    if not args.mac:
        print("Error: provide at least one MAC address")
        return

    for mac in args.mac:
        if len(mac) == 16:
            try:
                mac_bytes = bytes.fromhex(mac)
                mac = mac_bytes.decode("latin-1")
            except (ValueError, UnicodeDecodeError):
                print(f"Invalid hex MAC: {mac}")
                continue
        elif len(mac) != 8:
            print(f"Invalid MAC (must be 8 or 16 chars): {mac}")
            continue

        display = mac if mac.isascii() else "".join(f"{ord(c):02x}" for c in mac)
        print(f"Unpairing {display}…")
        try:
            dongle.delete(mac)
            print("  Removed")
        except TimeoutError:
            print(f"  Error: timed out while removing {display}")
        except AssertionError as err:
            print(f"  Error: {err}")


def cmd_fix(dongle: dp.Dongle, _args) -> None:
    """Attempt to remove known-bad (corrupt/null) sensor MACs from the dongle."""
    bad_macs = [
        "00000000",
        "\x00\x00\x00\x00\x00\x00\x00\x00",
        "ffffffffffffffff",
    ]
    print("Removing known-bad sensor MACs…")
    for mac in bad_macs:
        try:
            dongle.delete(mac)
            display = mac if mac.isascii() else dp.bytes_to_hex(mac.encode("latin-1"))
            print(f"  Removed: {display}")
        except (TimeoutError, AssertionError, Exception):
            pass  # expected – bad MACs often don't respond cleanly
    print("Done")


def cmd_chime(dongle: dp.Dongle, args) -> None:
    """Play a chime on a paired chime sensor.

    Args: mac ring_id repeat_count volume
    """
    dongle.play_chime(args.mac, int(args.ring_id), int(args.repeat_count), int(args.volume))
    print("Chime sent")


def cmd_raw(dongle: dp.Dongle, args) -> None:
    """Send a raw hex packet to the dongle (diagnostic use).

    Bytes should be comma-separated hex values, e.g. 'aa,55,43,05,27,00,6f'
    """
    data = bytes(int(x, 16) for x in args.bytes.strip().split(","))
    print(f"Sending: {dp.bytes_to_hex(data)}")
    dongle.send_raw(data)


def cmd_monitor(dongle: dp.Dongle, args) -> None:
    """Monitor and print sensor events (runs until Ctrl-C)."""
    import time

    print("Monitoring sensor events – press Ctrl-C to stop")
    try:
        while True:
            dongle.check_error()
            time.sleep(1)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WyzeSense bridge tool – direct USB dongle management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--device",
        default="auto",
        metavar="PATH|auto",
        help="USB HID device path, or 'auto' to detect automatically (default: auto)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--verbose", action="store_true", help="Extra verbose output (with --debug)")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List paired sensors")
    sub.add_parser("pair", help="Pair a new sensor (scan mode)")
    sub.add_parser("monitor", help="Monitor and print live sensor events")
    sub.add_parser("fix", help="Remove known-bad corrupt sensor MACs")

    unpair_p = sub.add_parser("unpair", help="Unpair sensor(s) by MAC address")
    unpair_p.add_argument("mac", nargs="+", metavar="MAC", help="8-char or 16-char hex MAC address(es)")

    chime_p = sub.add_parser("chime", help="Play a chime on a chime sensor")
    chime_p.add_argument("mac", help="Sensor MAC address")
    chime_p.add_argument("ring_id", help="Ring ID")
    chime_p.add_argument("repeat_count", help="Repeat count")
    chime_p.add_argument("volume", help="Volume (1–9)")

    raw_p = sub.add_parser("raw", help="Send raw hex bytes to the dongle (diagnostic)")
    raw_p.add_argument("bytes", help="Comma-separated hex bytes, e.g. 'aa,55,43,05,27,00,6f'")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_COMMANDS = {
    "list": cmd_list,
    "pair": cmd_pair,
    "unpair": cmd_unpair,
    "fix": cmd_fix,
    "chime": cmd_chime,
    "raw": cmd_raw,
    "monitor": cmd_monitor,
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    log_level = logging.WARNING
    if args.debug:
        log_level = logging.DEBUG - (1 if args.verbose else 0)
    logging.basicConfig(level=log_level, format="%(levelname)s %(asctime)s %(message)s")

    device = args.device
    if device.lower() == "auto":
        import sys

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        try:
            from config import find_dongle_device

            device = find_dongle_device()
        except ImportError:
            device = None
        if not device:
            print("Error: no WyzeSense dongle found automatically. Try --device /dev/hidrawN")
            return 2

    print(f"Opening dongle at {device}…")
    try:
        dongle = dp.open_dongle(device, _on_event, logging.getLogger("ws2m.dongle"))
    except OSError:
        print(f"Error: could not open device at {device}")
        return 2

    print("Dongle info:")
    print(f"  MAC:  {dongle.mac}")
    print(f"  VER:  {dongle.version}")
    print(f"  ENR:  {binascii.hexlify(dongle.enr).decode()}")

    handler = _COMMANDS.get(args.command)
    if handler is None:
        print(f"Unknown command: {args.command}")
        dongle.stop()
        return 1

    try:
        handler(dongle, args)
    finally:
        dongle.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
