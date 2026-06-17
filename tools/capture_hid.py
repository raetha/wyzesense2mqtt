#!/usr/bin/env python3
"""
WyzeSense HID frame capture tool.

Reads raw frames directly from the USB HID device (bypassing the bridge
service entirely) and writes them to a length-prefixed binary file suitable
for use as a test fixture.

The bridge service MUST NOT be running while this script is active — both
cannot hold the device open at the same time.

Usage
-----
    python3 tools/capture_hid.py [--device PATH] [--duration SECONDS] [--output PATH]

Options
-------
    --device PATH       HID device path [default: /dev/hidraw0]
    --duration SECONDS  How long to capture [default: 60]
    --output PATH       Output file [default: tests/fixtures/hid_capture.bin]

After capture the script prompts you to enter the real MAC addresses seen in
the data so it can replace them with obfuscated stand-ins before saving.
This keeps sensor identifiers out of the repository.

Output format
-------------
Each frame is stored as:
    [1 byte: frame length N][N bytes: raw HID payload]

This matches what _read_raw_hid() returns from the dongle worker, one record
per os.read() call.  The test fixture reader in tests/test_fixtures.py
expects exactly this format.
"""

import argparse
import os
import re
import sys
import time


def read_frames(device: str, duration: int) -> list[bytes]:
    """Open *device* directly and collect raw HID frames for *duration* seconds."""
    print(f"Opening {device}...")
    try:
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except OSError as err:
        print(f"Error: cannot open {device}: {err}")
        print("Is the bridge service still running?  Stop it first.")
        sys.exit(1)

    frames = []
    deadline = time.time() + duration
    print(f"Capturing for {duration}s — trigger your sensors now...")

    try:
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            print(f"\r  {remaining:3d}s remaining  {len(frames):4d} frames captured", end="", flush=True)
            try:
                data = os.read(fd, 0x40)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            except OSError as err:
                print(f"\nRead error: {err}")
                break

            if not data:
                time.sleep(0.05)
                continue

            data = bytes(data)
            length = data[0]
            if length == 0 or length > 0x3F:
                time.sleep(0.05)
                continue

            frame = data[1 : 1 + length]
            frames.append(frame)

    except KeyboardInterrupt:
        print("\nCapture interrupted by user")
    finally:
        os.close(fd)
        print()

    print(f"Captured {len(frames)} frame(s)")
    return frames


def find_macs_in_frames(frames: list[bytes]) -> list[str]:
    """Scan frames for 8-byte ASCII sequences that look like sensor MACs.

    Returns a deduplicated list in order of first appearance.
    """
    seen = []
    for frame in frames:
        # Look for printable ASCII runs of exactly 8 chars that could be MACs
        for i in range(len(frame) - 7):
            candidate = frame[i : i + 8]
            try:
                mac = candidate.decode("ascii")
            except UnicodeDecodeError:
                continue
            if re.match(r'^[A-Za-z0-9]{8}$', mac) and mac not in seen:
                # Exclude obvious non-MACs (firmware version strings etc.)
                if not mac.startswith("Ok5HPNQ") and mac != "00000000":
                    seen.append(mac)

    return seen


def obfuscate(frames: list[bytes], replacements: dict[str, str]) -> list[bytes]:
    """Replace every occurrence of each real MAC bytes with its stand-in."""
    result = []
    for frame in frames:
        data = frame
        for real, fake in replacements.items():
            try:
                real_bytes = real.encode("ascii")
                fake_bytes = fake.encode("ascii")
                data = data.replace(real_bytes, fake_bytes)
            except (UnicodeEncodeError, ValueError):
                pass
        result.append(data)
    return result


def write_capture(path: str, frames: list[bytes]) -> None:
    """Write frames to *path* in length-prefixed format."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        for frame in frames:
            length = min(len(frame), 0xFF)
            f.write(bytes([length]))
            f.write(frame[:length])
    size = os.path.getsize(path)
    print(f"Wrote {len(frames)} frame(s) ({size} bytes) to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture WyzeSense HID frames for test fixtures",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[0].strip(),
    )
    parser.add_argument("--device", default="/dev/hidraw0", metavar="PATH",
                        help="HID device path [default: /dev/hidraw0]")
    parser.add_argument("--duration", type=int, default=60, metavar="SECONDS",
                        help="Capture duration in seconds [default: 60]")
    parser.add_argument("--output", default="tests/fixtures/hid_capture.bin", metavar="PATH",
                        help="Output file [default: tests/fixtures/hid_capture.bin]")
    parser.add_argument("--no-obfuscate", action="store_true",
                        help="Skip MAC obfuscation prompts (not recommended for commits)")
    args = parser.parse_args()

    frames = read_frames(args.device, args.duration)

    if not frames:
        print("No frames captured — nothing to save")
        sys.exit(1)

    if not args.no_obfuscate:
        detected = find_macs_in_frames(frames)
        if detected:
            print(f"\nDetected {len(detected)} possible MAC address(es) in capture:")
            for mac in detected:
                print(f"  {mac}")
            print()

            # Build replacement map interactively
            placeholders = ["AAAAAAAA", "BBBBBBBB", "CCCCCCCC", "DDDDDDDD",
                            "EEEEEEEE", "FFFFFFFF", "GGGGGGGG", "HHHHHHHH"]
            replacements: dict[str, str] = {}

            print("For each MAC, press Enter to replace it with the next placeholder,")
            print("or type a custom 8-char replacement, or type 'skip' to leave it as-is.\n")

            placeholder_idx = 0
            for mac in detected:
                default = placeholders[placeholder_idx] if placeholder_idx < len(placeholders) else f"SENSOR{placeholder_idx:02d}"
                response = input(f"  Replace {mac!r} with [{default}]: ").strip()
                if response.lower() == "skip":
                    print(f"    Keeping {mac!r} as-is")
                    continue
                replacement = response if response else default
                if len(replacement) != 8:
                    print(f"    Warning: {replacement!r} is not 8 chars — skipping")
                    continue
                replacements[mac] = replacement
                print(f"    {mac!r} → {replacement!r}")
                placeholder_idx += 1

            if replacements:
                frames = obfuscate(frames, replacements)
                print(f"\nObfuscated {len(replacements)} MAC(s)")
            else:
                print("No replacements made")
        else:
            print("No MAC addresses detected in capture (this may be normal for init-only captures)")

        # Final warning
        print()
        choice = input("Save capture? [Y/n]: ").strip().lower()
        if choice == "n":
            print("Aborted — nothing saved")
            sys.exit(0)

    write_capture(args.output, frames)

    print()
    print("Next steps:")
    print(f"  1. Copy {args.output} into tests/fixtures/ if it isn't there already")
    print("  2. Add expected event assertions to EXPECTED_EVENTS in tests/test_fixtures.py")
    print("  3. Run: pytest tests/test_fixtures.py -v")


if __name__ == "__main__":
    main()
