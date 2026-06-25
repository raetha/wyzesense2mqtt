# Contributing protocol data and fixes

ws2m talks to the WyzeSense USB dongle over a USB HID protocol that has been
reverse engineered by the community — there is no official specification. When
a sensor or feature does not work correctly, the most valuable thing a
contributor can do is capture raw HID traffic and share it so the packet format
can be decoded.

This document explains how to do that, what to include in an issue or PR, and
how the codebase translates raw bytes into sensor events.

---

## Before you start

- **Stop the ws2m bridge** before running any capture or diagnostic tool. The
  bridge holds the HID device open exclusively, and a second process cannot
  open it at the same time.
- If you are running ws2m as a Docker container: `docker stop wyzesense2mqtt`
- If you are running it under systemd: `sudo systemctl stop wyzesense2mqtt`

---

## Tools

### 1. `tools/capture_hid.py` — raw frame capture (best for bug reports)

This is the primary tool for capturing protocol data. It reads raw HID frames
directly from the dongle for a set duration, then helps you replace real sensor
MAC addresses with anonymous placeholders before you share the file.

```bash
python3 tools/capture_hid.py --device /dev/hidraw0 --duration 60 --output capture.bin
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--device PATH` | `/dev/hidraw0` | HID device path. Run `ls /dev/hidraw*` to find yours. |
| `--duration SECONDS` | `60` | How long to capture. Use longer durations for infrequent events. |
| `--output PATH` | `tests/fixtures/hid_capture.bin` | Where to save the binary file. |
| `--no-obfuscate` | off | Skip MAC replacement prompts. **Do not use when sharing captures publicly.** |

After capture, the script detects likely MAC addresses in the data and walks
you through replacing them with safe placeholders before saving. Always do this
before attaching a capture to a GitHub issue.

**What to trigger during capture:**

Trigger the specific device event you want captured. For example, for keypad
feedback investigation:
1. Start the capture
2. Arm the system via the Wyze app (so the Wyze cloud sends a feedback signal)
3. Enter a PIN on the keypad
4. Disarm via the app again
5. Let the capture finish

The more context around the event of interest the better — capture idle traffic
before and after so frame boundaries are clear.

### 2. `python3 -m wyzesense2mqtt bridge_tool monitor` — live event stream

The `monitor` subcommand connects to the dongle and prints parsed sensor events
as they arrive. Useful for confirming that a sensor is being received at all
and checking what event type and fields ws2m is producing.

```bash
python3 -m wyzesense2mqtt bridge_tool --debug monitor
```

Add `--debug` to see raw packet bytes alongside parsed events. This is useful
for checking whether an unrecognised packet type is arriving (you will see an
`unknown:*` event with the raw hex payload in the log).

### 3. `python3 -m wyzesense2mqtt bridge_tool raw` — send raw packets

The `raw` subcommand sends arbitrary bytes to the dongle. This is for
**advanced investigation only** — sending unknown commands can confuse the
dongle and require a power cycle to recover.

```bash
python3 -m wyzesense2mqtt bridge_tool raw "aa,55,43,05,27,00,6f"
```

Bytes are comma-separated hex values. The tool handles framing — you provide
only the payload bytes, not the `0x55AA` magic prefix.

---

## How the protocol is structured

Understanding the packet format helps you interpret captures and write fixes.
All of this lives in `wyzesense2mqtt/dongle_protocol.py`.

### Packet framing

Every HID frame starts with a 2-byte magic: `0x55 0xAA` (host→dongle) or
`0xAA 0x55` (dongle→host). The full frame structure is:

```
[2 bytes: magic] [1 byte: cmd_type] [1 byte: cmd_id] [1 byte: length] [N bytes: payload] [1 byte: checksum]
```

The checksum is the XOR of all bytes from `cmd_type` through the last payload
byte.

`cmd_type` is either `0x43` (sync, expects an ACK) or `0x53` (async,
no ACK expected). `cmd_id` identifies the command within that type.

### Sensor event packets

There are two sensor event packet types:

| Packet | cmd_type | cmd_id | Used for |
|---|---|---|---|
| `NOTIFY_SENSOR_ALARM` | `0x53` | `0x19` | v1 sensors (contact, motion, climate) |
| `NOTIFY_SENSOR_ALARM2` | `0x53` | `0x55` | v2 sensors (leak, keypad, and newer v2 contact/motion) |

Inside a `NOTIFY_SENSOR_ALARM` payload:

```
[8 bytes: timestamp ms, big-endian uint64]
[1 byte: event type  — 0xA1=heartbeat, 0xA2=alarm]
[8 bytes: MAC, ASCII]
[1 byte: sensor_type_id]
[N bytes: event data — structure varies by sensor_type_id and event type]
```

Inside a `NOTIFY_SENSOR_ALARM2` payload:

```
[1 byte: event type  — 0xEA=leak, 0xE8=climate; 0x00 for keypad]
[8 bytes: MAC, ASCII]
[1 byte: sensor_type_id  — 0x05=keypad, 0x03=leak, 0x07=climate]
[N bytes: event data]
```

For the **keypad specifically**, `event_data` has its own internal structure:

```
[0]     total payload length (including this byte)
[1..4]  unknown / padding
[5]     sub-event type  — 0x02=mode, 0x0A=motion, 0x06=pin_start, 0x08=pin_confirm
[6]     state value  (meaning depends on sub-event type)
[7]     raw battery level (0–155 scale; divide by 155 × 100 for %)
[8]     signal strength (positive RSSI; ws2m negates it)
[9..]   PIN digits, ASCII (only present for sub-event 0x08)
```

### Adding support for an unknown packet type

When ws2m sees a packet it cannot parse, it produces a `SensorEvent` with
`event = "unknown:<hex>"` and logs a warning with the raw bytes. To add
support:

1. Identify the packet from the hex dump in the log or a capture file.
2. Add a constant in `dongle_protocol.py` (e.g. `_KEYPAD_EVENT_FEEDBACK = 0x0C`).
3. Add a parser classmethod on `SensorEvent` following the pattern of
   `_parse_keypad`, `_parse_leak`, etc.
4. Register it in the appropriate dispatch table inside `from_packet` or
   `from_packet_v2`.
5. Handle the new event type in `bridge.py` `DongleWorker._on_dongle_event`.
6. Add tests in `tests/test_dongle_protocol.py` using synthetic payloads built
   with a `make_*_payload` helper in `tests/conftest.py`.

---

## Specific verification wanted: keypad display feedback

The keypad has a backlit display and a speaker.  ws2m sends `CMD_SEND_KEYPAD_EVENT`
(`0x53/0x53`) to the dongle whenever HA/Alarmo pushes a new alarm state via
the command topic.  The packet structure was confirmed from
[AK5nowman/WyzeSense](https://github.com/AK5nowman/WyzeSense) (reverse
engineered via Ghidra), but the actual effect on the physical keypad has not
been verified with hardware.

### What we know

- `CMD_SEND_KEYPAD_EVENT` (`ASYNC`, `cmd_id=0x53`) with a fixed payload
  structure including a state byte drives the keypad over RF.
- State byte values: `0x01`=disarmed, `0x02`=armed_home, `0x03`=armed_away,
  `0x05`=triggered.
- The C# source comment notes state `0x04` as "inactive(?)" — meaning uncertain.

### What we need confirmed

- Does `CMD_SEND_KEYPAD_EVENT` cause the keypad display to change?
- Does it trigger any audible feedback (beep, tone)?
- Is state byte `0x04` ever used, and what does it do?
- Sub-event `0x0C` (received from the keypad) is logged as "Some sort of alarm
  event?" in AK5nowman's code.  What triggers it?

### How to test

With ws2m running and a keypad paired:

1. Set `log_level: debug` in `config.yaml`.
2. Arm and disarm via HA/Alarmo and watch the keypad for any display or
   audible change.
3. Check the ws2m log for lines containing `Sent keypad status` — these
   confirm the command was sent.
4. Report what you observe (display changed? beep? nothing?) in a GitHub issue.

If you want to capture the raw HID traffic to verify the packet reaches the
dongle, use `capture_hid.py` as described above.

---

## Adding test fixtures

When you have a capture that reproduces a bug or exercises new functionality,
add it as a test fixture so the behaviour is locked in permanently:

1. Copy the `.bin` file to `tests/fixtures/`.
2. Open `tests/test_fixtures.py` and add entries to `EXPECTED_EVENTS` for the
   events your capture contains.
3. Run `pytest tests/test_fixtures.py -v` to confirm the fixture parses
   correctly.
4. Include the fixture file and updated test in your PR.

Fixture files must have real MACs replaced with placeholders — the capture
tool handles this interactively. If a fixture was captured without
obfuscation, use `tools/capture_hid.py --no-obfuscate` to re-save with
replacement applied, or edit the binary manually.

---

## Opening an issue

When reporting a protocol-related bug (wrong values, unrecognised packet,
sensor not detected), please include:

- ws2m version (`docker inspect` image tag, or `VERSION` in `config.py`)
- Sensor model and hardware version (printed on the sensor label)
- The relevant section of the ws2m log with `log_level: debug` set in
  `config.yaml` — look for lines containing `unknown:` or the sensor MAC
- A HID capture if you can get one (attach the `.bin` file to the issue)

The more raw data you can share, the faster a fix can be written and tested
without needing access to the physical hardware.
