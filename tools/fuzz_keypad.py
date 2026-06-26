#!/usr/bin/env python3
"""
Keypad feedback command fuzzer.

Systematically sends candidate commands to a paired Wyze Sense V2 Keypad,
watching for any visual (backlight) or audible (beep) response that indicates
a feedback packet has been found.  All commands sent are written to a log file
so that a successful combination can be identified even if you miss the exact
moment it fires.

The script works in two passes:

  Pass 1 — cmd_id sweep
    Sends every plausible command ID (0x00–0xFF) with a minimal fixed payload
    of [mac][0x01] for both SYNC (0x43) and ASYNC (0x53) packet types.  The
    goal is to find which cmd_id values produce any response from the keypad.

  Pass 2 — payload sweep (run after Pass 1 identifies a promising cmd_id)
    Exhaustively tries short payload byte sequences for a given cmd_type and
    cmd_id.  Useful once Pass 1 finds a candidate.

See docs/protocol.md for background on the packet format and
what we are looking for.

REQUIREMENTS
------------
  - Python 3.10+
  - The ws2m bridge must NOT be running (it holds the device exclusively)
  - The keypad must already be paired to the dongle
  - Run as root, or ensure your user has read/write access to /dev/hidrawN

USAGE
-----
  # Find the dongle device (usually /dev/hidraw0 or /dev/hidraw1)
  ls /dev/hidraw*

  # Identify the keypad MAC (run bridge_tool list while bridge is stopped)
  python3 -m wyzesense2mqtt bridge_tool list

  # Pass 1 – sweep all command IDs (watch the keypad while this runs)
  python3 tools/fuzz_keypad.py --device /dev/hidraw0 --mac KPADKPAD

  # Pass 2 – try payload variations for a specific cmd_type and cmd_id
  python3 tools/fuzz_keypad.py --device /dev/hidraw0 --mac KPADKPAD \\
      --pass2 --cmd-type 0x53 --cmd-id 0x71 --payload-len 1

  # Narrow Pass 1 to a specific range of cmd_id values
  python3 tools/fuzz_keypad.py --device /dev/hidraw0 --mac KPADKPAD \\
      --id-start 0x60 --id-end 0x80

WHAT TO WATCH FOR
-----------------
When a command triggers a keypad response you will see or hear:
  - A short beep or tone from the speaker
  - A change in backlight colour or brightness
  - The keypad display changing

When you notice a reaction, immediately check the log file (--log-file, default
fuzz_keypad.log) — the last entry before the reaction is the candidate command.
The script also prints every command to stdout so you can scroll back.

READING THE LOG
---------------
Each line has the format:
  [timestamp] SENT cmd_type=0xNN cmd_id=0xNN payload=XX XX XX ... raw=55AA...

The "raw" field is the complete framed packet as sent to the HID device.  You
can use this directly with bridge_tool raw to reproduce the command:

  python3 -m wyzesense2mqtt bridge_tool raw "55,AA,43,05,27,00,6f"

SAFETY
------
A small number of known-dangerous cmd_id values are skipped by default to
avoid accidentally deleting your sensors.  These are listed in SKIP_CMD_IDS
below.  Pass --no-skip to override (not recommended unless you know what you
are doing and have a backup of sensors.yaml).

The inter-command delay (--delay, default 0.4 s) gives the dongle time to
process each command and lets you see keypad reactions before the next command
fires.  If the dongle stops responding, increase the delay.

SHARING RESULTS
---------------
If you find a command that triggers keypad feedback, please open a GitHub issue
and include:
  - The full log file (fuzz_keypad.log)
  - Which line in the log corresponds to the reaction you observed
  - What the reaction looked like (beep? LED colour? both?)
  - Your dongle firmware version (shown at script startup)

See docs/protocol.md for more detail on what to include.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import os
import struct
import sys
import time

# ---------------------------------------------------------------------------
# Known cmd_id values — skip these to avoid side effects
# ---------------------------------------------------------------------------

# Maps (cmd_type, cmd_id) → description of what the command does.
# These are skipped in Pass 1 unless --no-skip is given.
KNOWN_COMMANDS: dict[tuple[int, int], str] = {
    (0x43, 0x02): "CMD_GET_ENR",
    (0x43, 0x04): "CMD_GET_MAC",
    (0x43, 0x06): "CMD_GET_KEY",
    (0x43, 0x0E): "CMD_SET_CH554_UPGRADE",
    (0x43, 0x12): "CMD_UPDATE_CC1310",
    (0x43, 0x27): "CMD_INQUIRY",
    (0x53, 0x14): "CMD_FINISH_AUTH",
    (0x53, 0x16): "CMD_GET_DONGLE_VERSION",
    (0x53, 0x19): "NOTIFY_SENSOR_ALARM",
    (0x53, 0x1C): "CMD_START_STOP_SCAN",
    (0x53, 0x20): "NOTIFY_SENSOR_SCAN",
    (0x53, 0x21): "CMD_GET_SENSOR_R1",
    (0x53, 0x23): "CMD_VERIFY_SENSOR",
    (0x53, 0x25): "CMD_DEL_SENSOR",
    (0x53, 0x2E): "CMD_GET_SENSOR_COUNT",
    (0x53, 0x30): "CMD_GET_SENSOR_LIST",
    (0x53, 0x32): "NOTIFY_SYNC_TIME",
    (0x53, 0x35): "NOTIFY_EVENT_LOG",
    (0x53, 0x53): "CMD_SEND_KEYPAD_EVENT (pushes alarm state to keypad display)",
    (0x53, 0x55): "NOTIFY_SENSOR_ALARM2 / KeyPadEvent (receive only)",
    (0x53, 0x70): "CMD_PLAY_CHIME",
    (0x53, 0xFF): "ASYNC_ACK",
}

# These are dangerous enough to always skip regardless of --no-skip.
ALWAYS_SKIP: set[tuple[int, int]] = {
    (0x53, 0x3F),  # CMD_DEL_ALL_SENSORS — deletes every paired sensor
}

# ---------------------------------------------------------------------------
# Minimal packet builder (no dependency on the wyzesense2mqtt package)
# ---------------------------------------------------------------------------
# The fuzzer is designed to be runnable standalone without installing ws2m,
# so we reimplement the framing logic here rather than importing it.


def _checksum(data: bytes) -> int:
    """XOR checksum of all bytes."""
    result = 0
    for b in data:
        result ^= b
    return result


def build_packet(cmd_type: int, cmd_id: int, payload: bytes) -> bytes:
    """Build a complete framed HID packet ready to write to the device.

    Frame layout:
      [0x55, 0xAA]  magic
      [cmd_type]    0x43 = sync, 0x53 = async
      [length]      len(payload) + 3  (covers cmd_type, cmd_id, payload)
      [cmd_id]
      [payload...]
      [cs_hi, cs_lo]  big-endian XOR checksum of cmd_type..payload
    """
    inner = bytes([cmd_type, len(payload) + 3, cmd_id]) + payload
    cs = _checksum(inner)
    pkt = b"\x55\xaa" + inner + struct.pack(">H", cs)
    # HID write must be exactly 0x40 bytes, zero-padded
    return pkt + b"\x00" * (0x40 - len(pkt))


def send_packet(fd: int, cmd_type: int, cmd_id: int, payload: bytes) -> bytes:
    """Build and write a packet; return the raw bytes sent (for logging)."""
    pkt = build_packet(cmd_type, cmd_id, payload)
    os.write(fd, pkt)
    return pkt


# ---------------------------------------------------------------------------
# Dongle health check
# ---------------------------------------------------------------------------

_INQUIRY_PAYLOAD = bytes([0x00])  # CMD_INQUIRY (0x43, 0x27) with empty-ish payload


def probe_dongle(fd: int, logger: logging.Logger) -> bool:
    """Send CMD_INQUIRY and check for any response within 0.5 s.

    Returns True if the dongle appears alive, False if it has stopped
    responding (which may indicate it needs a power cycle).
    """
    try:
        send_packet(fd, 0x43, 0x27, _INQUIRY_PAYLOAD)
        deadline = time.time() + 0.5
        while time.time() < deadline:
            try:
                data = os.read(fd, 0x40)
                if data and len(data) > 2 and data[0:2] in (b"\x55\xaa", b"\xaa\x55"):
                    return True
            except BlockingIOError:
                time.sleep(0.05)
        logger.warning("No response to probe — dongle may have stalled")
        return False
    except OSError as exc:
        logger.error("Probe write failed: %s", exc)
        return False


def drain(fd: int, timeout: float = 0.2) -> None:
    """Discard any pending bytes from the device (non-blocking)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.read(fd, 0x40)
        except BlockingIOError:
            break


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("fuzz_keypad")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


def log_command(
    logger: logging.Logger,
    cmd_type: int,
    cmd_id: int,
    payload: bytes,
    raw: bytes,
    note: str = "",
) -> None:
    payload_hex = " ".join(f"{b:02X}" for b in payload)
    raw_hex = "".join(f"{b:02X}" for b in raw[:16])  # first 16 bytes is plenty
    extra = f"  # {note}" if note else ""
    logger.debug(
        "SENT cmd_type=0x%02X cmd_id=0x%02X payload=%s raw=%s%s",
        cmd_type,
        cmd_id,
        payload_hex or "(empty)",
        raw_hex,
        extra,
    )
    # Also print to stdout at INFO so the human watching can see it
    logger.info(
        "  → cmd_type=0x%02X cmd_id=0x%02X payload=[%s]%s",
        cmd_type,
        cmd_id,
        payload_hex or "empty",
        f"  ({note})" if note else "",
    )


# ---------------------------------------------------------------------------
# Pass 1 — command ID sweep
# ---------------------------------------------------------------------------


def pass1_cmd_sweep(
    fd: int,
    mac: str,
    logger: logging.Logger,
    id_start: int,
    id_end: int,
    delay: float,
    no_skip: bool,
    probe_interval: int,
) -> None:
    """Sweep every cmd_id from id_start to id_end for both SYNC and ASYNC types."""
    mac_bytes = mac.encode("ascii")
    # Minimal payload: MAC + one status byte 0x01 (a reasonable "armed_away" guess)
    base_payload = mac_bytes + bytes([0x01])

    cmd_types = [0x43, 0x53]  # SYNC, ASYNC
    total = (id_end - id_start + 1) * len(cmd_types)
    sent = 0

    logger.info("=" * 60)
    logger.info("Pass 1: cmd_id sweep 0x%02X–0x%02X for %s", id_start, id_end, mac)
    logger.info("Total commands to send: %d", total)
    logger.info("WATCH THE KEYPAD and listen for any beep or backlight change.")
    logger.info("Check %s for the command that triggered the reaction.", logger.handlers[1].baseFilename)
    logger.info("Press Ctrl-C at any time to stop.")
    logger.info("=" * 60)
    time.sleep(2)  # brief pause so the user can read the header

    for cmd_type in cmd_types:
        type_name = "SYNC" if cmd_type == 0x43 else "ASYNC"
        logger.info("")
        logger.info("--- %s (0x%02X) ---", type_name, cmd_type)

        for cmd_id in range(id_start, id_end + 1):
            key = (cmd_type, cmd_id)

            if key in ALWAYS_SKIP:
                logger.info("  SKIP 0x%02X — always skipped (%s)", cmd_id, KNOWN_COMMANDS.get(key, ""))
                continue

            note = ""
            if key in KNOWN_COMMANDS:
                note = KNOWN_COMMANDS[key]
                if not no_skip:
                    logger.info("  SKIP 0x%02X — known command: %s", cmd_id, note)
                    continue
                note = f"known: {note}"

            try:
                raw = send_packet(fd, cmd_type, cmd_id, base_payload)
                log_command(logger, cmd_type, cmd_id, base_payload, raw, note)
                drain(fd)
                sent += 1
            except OSError as exc:
                logger.error("  Write failed at cmd_id=0x%02X: %s — stopping", cmd_id, exc)
                return

            time.sleep(delay)

            # Periodic dongle health check
            if sent % probe_interval == 0:
                alive = probe_dongle(fd, logger)
                if not alive:
                    logger.warning(
                        "Dongle not responding after %d commands. "
                        "Try power-cycling the dongle and restarting Pass 1 "
                        "from --id-start 0x%02X.",
                        sent,
                        cmd_id,
                    )
                    return
                drain(fd)

    logger.info("")
    logger.info("Pass 1 complete — %d commands sent.", sent)
    logger.info(
        "If you saw a reaction, check the log for the command just before it. "
        "Then run Pass 2 with --pass2 --cmd-type 0xNN --cmd-id 0xNN to explore variants."
    )


# ---------------------------------------------------------------------------
# Pass 2 — payload sweep for a specific cmd_type + cmd_id
# ---------------------------------------------------------------------------


def pass2_payload_sweep(
    fd: int,
    mac: str,
    logger: logging.Logger,
    cmd_type: int,
    cmd_id: int,
    payload_len: int,
    delay: float,
    probe_interval: int,
) -> None:
    """Try every possible payload of length payload_len for the given command.

    The payload always starts with the keypad MAC (8 bytes), followed by
    payload_len bytes of the search space (0x00–0xFF each).

    With payload_len=1 this is 256 commands.
    With payload_len=2 this is 65,536 commands (~7 hours at 0.4 s delay).
    With payload_len=3 this is 16,777,216 — not practical; use targeted ranges.
    """
    mac_bytes = mac.encode("ascii")
    total = 256 ** payload_len
    sent = 0

    logger.info("=" * 60)
    logger.info(
        "Pass 2: payload sweep for cmd_type=0x%02X cmd_id=0x%02X payload_len=%d",
        cmd_type,
        cmd_id,
        payload_len,
    )
    logger.info("Search space: %d combinations", total)
    eta_hours = (total * delay) / 3600
    if eta_hours > 1:
        logger.info("Estimated time: %.1f hours — consider narrowing with --payload-len 1 first.", eta_hours)
    logger.info("WATCH THE KEYPAD. Press Ctrl-C to stop.")
    logger.info("=" * 60)
    time.sleep(2)

    for combo in itertools.product(range(256), repeat=payload_len):
        payload = mac_bytes + bytes(combo)

        try:
            raw = send_packet(fd, cmd_type, cmd_id, payload)
            log_command(logger, cmd_type, cmd_id, payload, raw)
            drain(fd)
            sent += 1
        except OSError as exc:
            logger.error("Write failed: %s — stopping", exc)
            return

        time.sleep(delay)

        if sent % probe_interval == 0:
            alive = probe_dongle(fd, logger)
            if not alive:
                last_combo = " ".join(f"{b:02X}" for b in combo)
                logger.warning(
                    "Dongle not responding after %d commands. Last payload suffix: [%s]",
                    sent,
                    last_combo,
                )
                return
            drain(fd)

    logger.info("Pass 2 complete — %d commands sent.", sent)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def open_device(device: str) -> int:
    """Open the HID device in non-blocking read/write mode."""
    try:
        return os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except PermissionError:
        print(f"Error: cannot open {device} — permission denied.")
        print("Try:  sudo python3 tools/fuzz_keypad.py ...")
        print("Or add your user to the 'input' group and log out/in.")
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: device {device} not found.")
        print("Run 'ls /dev/hidraw*' to find the correct path.")
        sys.exit(1)
    except OSError as exc:
        print(f"Error: cannot open {device}: {exc}")
        sys.exit(1)


def validate_mac(mac: str) -> str:
    if len(mac) != 8 or not mac.isalnum():
        print(f"Error: MAC must be exactly 8 alphanumeric characters, got {mac!r}")
        sys.exit(1)
    return mac.upper()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wyze Sense Keypad feedback command fuzzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("REQUIREMENTS")[0].strip(),
    )

    parser.add_argument(
        "--device", default="/dev/hidraw0", metavar="PATH",
        help="HID device path [default: /dev/hidraw0]",
    )
    parser.add_argument(
        "--mac", required=True, metavar="MAC",
        help="Keypad MAC address (8 alphanumeric chars, e.g. KPADKPAD). "
             "Run 'python3 -m wyzesense2mqtt bridge_tool list' to find it.",
    )
    parser.add_argument(
        "--log-file", default="fuzz_keypad.log", metavar="PATH",
        help="Log file for all commands sent [default: fuzz_keypad.log]",
    )
    parser.add_argument(
        "--delay", type=float, default=0.4, metavar="SECONDS",
        help="Delay between commands in seconds [default: 0.4]. "
             "Increase if the dongle stops responding.",
    )
    parser.add_argument(
        "--probe-interval", type=int, default=20, metavar="N",
        help="Check dongle health every N commands [default: 20]",
    )
    parser.add_argument(
        "--no-skip", action="store_true",
        help="Do not skip known command IDs in Pass 1. "
             "Useful if you want to observe known-command responses for comparison. "
             "CMD_DEL_ALL_SENSORS is always skipped regardless.",
    )

    # Pass 1 options
    pass1 = parser.add_argument_group("Pass 1 options (cmd_id sweep, default mode)")
    pass1.add_argument(
        "--id-start", type=lambda x: int(x, 0), default=0x00, metavar="HEX",
        help="First cmd_id to try [default: 0x00]",
    )
    pass1.add_argument(
        "--id-end", type=lambda x: int(x, 0), default=0xFF, metavar="HEX",
        help="Last cmd_id to try [default: 0xFF]",
    )

    # Pass 2 options
    pass2 = parser.add_argument_group("Pass 2 options (payload sweep for a specific command)")
    pass2.add_argument(
        "--pass2", action="store_true",
        help="Run Pass 2 instead of Pass 1",
    )
    pass2.add_argument(
        "--cmd-type", type=lambda x: int(x, 0), metavar="HEX",
        help="Command type byte: 0x43 (SYNC) or 0x53 (ASYNC)",
    )
    pass2.add_argument(
        "--cmd-id", type=lambda x: int(x, 0), metavar="HEX",
        help="Command ID byte to sweep payloads for",
    )
    pass2.add_argument(
        "--payload-len", type=int, default=1, metavar="N",
        help="Number of payload bytes after the MAC to sweep [default: 1]. "
             "1 = 256 combos, 2 = 65536, 3 = 16M (impractical).",
    )

    args = parser.parse_args()
    mac = validate_mac(args.mac)
    logger = setup_logging(args.log_file)

    if args.pass2 and (args.cmd_type is None or args.cmd_id is None):
        parser.error("--pass2 requires --cmd-type and --cmd-id")

    logger.info("Keypad feedback fuzzer starting")
    logger.info("Device:   %s", args.device)
    logger.info("Keypad MAC: %s", mac)
    logger.info("Log file: %s", args.log_file)
    logger.info("")
    logger.info("*** MAKE SURE the ws2m bridge is NOT running ***")
    logger.info("    docker stop wyzesense2mqtt   OR")
    logger.info("    sudo systemctl stop wyzesense2mqtt")
    logger.info("")

    fd = open_device(args.device)

    # Initial health check
    logger.info("Checking dongle is alive...")
    if not probe_dongle(fd, logger):
        logger.error("Dongle not responding to initial probe. Is the bridge still running?")
        os.close(fd)
        sys.exit(1)
    drain(fd)
    logger.info("Dongle OK.")
    logger.info("")

    try:
        if args.pass2:
            pass2_payload_sweep(
                fd=fd,
                mac=mac,
                logger=logger,
                cmd_type=args.cmd_type,
                cmd_id=args.cmd_id,
                payload_len=args.payload_len,
                delay=args.delay,
                probe_interval=args.probe_interval,
            )
        else:
            pass1_cmd_sweep(
                fd=fd,
                mac=mac,
                logger=logger,
                id_start=args.id_start,
                id_end=args.id_end,
                delay=args.delay,
                no_skip=args.no_skip,
                probe_interval=args.probe_interval,
            )
    except KeyboardInterrupt:
        logger.info("")
        logger.info("Interrupted by user.")
        logger.info("Check %s for the last command sent before you stopped.", args.log_file)
    finally:
        os.close(fd)
        logger.info("Device closed. Done.")


if __name__ == "__main__":
    main()
