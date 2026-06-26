#!/usr/bin/env python3
"""
WyzeSense dongle tool – direct USB dongle CLI.

Provides command-line access to the WyzeSense USB dongle for pairing,
unpairing, listing sensors, and low-level diagnostics.  This tool
communicates directly with the dongle hardware and does **not** require
the bridge service or an MQTT broker to be running.

For MQTT/discovery maintenance, see cli/mqtt_tool.py instead.

Usage:
    python3 -m cli.dongle_tool [--device PATH] [--debug] [--verbose]

Options:
    --device PATH   Path to the USB HID device, or 'auto' to detect automatically [default: auto]
    --debug         Enable debug logging
    --verbose       Increase log verbosity (combined with --debug)

When 'auto' is used and multiple dongles are detected, an interactive
prompt lets you select which dongle to use.  The prompt shows the device
path and any config-derived info (dongle MAC, sensor count) for each.
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
    """Remove invalid (corrupt/null) sensor MACs from dongle NVRAM.

    Fetches the actual paired sensor list from the dongle, identifies entries
    with invalid MACs (all-zero, all-0xFF, non-printable ASCII), and removes
    them.  Healthy sensors are left untouched.
    """
    print("Fetching paired sensor list from dongle…")
    try:
        macs = dongle.list()
    except (TimeoutError, Exception) as exc:
        print(f"  Error: could not retrieve sensor list — {exc}")
        return

    if not macs:
        print("  No sensors paired — nothing to fix.")
        return

    def _is_invalid(mac: str) -> bool:
        b = mac.encode("latin-1")
        return all(c == 0x00 for c in b) or all(c == 0xFF for c in b) or not all(0x20 <= c < 0x7F for c in b)

    invalid = [m for m in macs if _is_invalid(m)]
    valid = [m for m in macs if not _is_invalid(m)]

    print(f"  Found {len(macs)} paired sensor(s): {len(valid)} valid, {len(invalid)} invalid.")

    if not invalid:
        print("  No corrupt entries found — nothing to remove.")
        return

    for mac in invalid:
        display = dp.bytes_to_hex(mac.encode("latin-1"))
        try:
            dongle.delete(mac)
            print(f"  Removed: {display}")
        except (TimeoutError, AssertionError, Exception) as exc:
            print(f"  Failed to remove {display}: {exc}")

    print("Done.")


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
        description="WyzeSense dongle tool – direct USB dongle management",
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


def _select_dongle(devices: list[str]) -> str | None:
    """Present an interactive selection when multiple dongles are detected.

    Shows device path and any config-derived info (MAC, sensor count) for each
    dongle so the user can identify which one to target without needing to know
    the hidraw path.  Returns the selected device path, or None on cancel.
    """
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    # Try to load per-dongle config metadata for each device.
    # We can't know a dongle's MAC without opening it, but if a dongles/<mac>/
    # directory exists we can match by listing candidates and showing sensor counts.
    # For now we show what we have: device path + any known dongle dirs from config.
    dongle_info: list[dict] = []
    try:
        import os as _os

        from config import CONFIG_DIR, DONGLES_DIR
        from sensors import SensorRegistry

        dongles_dir = _os.path.join(CONFIG_DIR, DONGLES_DIR)
        known_macs: list[str] = []
        if _os.path.isdir(dongles_dir):
            known_macs = sorted(_os.listdir(dongles_dir))

        for i, path in enumerate(devices):
            info: dict = {"path": path, "index": i + 1}
            # If only one known dongle MAC exists in config, associate it with
            # the first device (best-effort — we can't map without opening).
            # When multiple MACs exist we show them separately.
            if len(known_macs) == 1 and len(devices) == 1:
                mac = known_macs[0]
                reg = SensorRegistry(mac)
                reg.load_sensors()
                info["mac"] = mac
                info["sensor_count"] = len(reg.sensors)
            elif i < len(known_macs):
                mac = known_macs[i]
                reg = SensorRegistry(mac)
                reg.load_sensors()
                info["mac"] = mac
                info["sensor_count"] = len(reg.sensors)
            dongle_info.append(info)
    except Exception:
        dongle_info = [{"path": d, "index": i + 1} for i, d in enumerate(devices)]

    print(f"\nFound {len(devices)} WyzeSense dongle(s):\n")
    for info in dongle_info:
        line = f"  [{info['index']}]  {info['path']}"
        if "mac" in info:
            line += f"  (MAC: {info['mac']}, {info['sensor_count']} sensor(s) in config)"
        print(line)

    print()
    while True:
        try:
            raw = input(f"Select dongle [1-{len(devices)}] or 'q' to quit: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() == "q":
            return None
        try:
            choice = int(raw)
            if 1 <= choice <= len(devices):
                return devices[choice - 1]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(devices)}, or 'q' to quit.")


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
            from config import find_all_dongle_devices

            devices = find_all_dongle_devices()
        except ImportError:
            devices = []

        if not devices:
            print("Error: no WyzeSense dongle found automatically. Try --device /dev/hidrawN")
            return 2
        elif len(devices) == 1:
            device = devices[0]
        else:
            device = _select_dongle(devices)
            if device is None:
                return 0

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
