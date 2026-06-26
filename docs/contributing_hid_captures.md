# Contributing HID Captures

The WyzeSense USB protocol has no official specification — it was reverse engineered from community observation of USB traffic. When a sensor or feature does not work correctly, the most useful thing you can contribute is a raw HID capture.

---

## Before you start

Stop the ws2m bridge before running any capture tool. The bridge holds the HID device open exclusively.

- Docker: `docker stop wyzesense2mqtt`
- systemd: `sudo systemctl stop wyzesense2mqtt`

---

## Capture tools

### `tools/capture_hid.py` — raw frame capture

Reads raw HID frames from the dongle for a set duration, then offers to anonymise MAC addresses before you share the file.

```bash
python3 tools/capture_hid.py --device /dev/hidraw0 --duration 60 --output capture.bin
```

| Flag | Default | Description |
|---|---|---|
| `--device PATH` | `/dev/hidraw0` | HID device path |
| `--duration SECONDS` | `60` | Capture duration |
| `--output PATH` | `tests/fixtures/hid_capture.bin` | Output file |
| `--no-obfuscate` | off | Skip MAC replacement. **Do not use when sharing publicly.** |

After capture the script detects likely MAC addresses and walks you through replacing them with safe placeholders. Always do this before attaching a capture to a GitHub issue.

**What to trigger:** reproduce the specific event you want captured. For example, for keypad feedback investigation:

1. Start the capture
2. Arm the system (via Wyze app so the cloud sends feedback)
3. Enter a PIN on the keypad
4. Disarm
5. Let the capture finish

The more context around the event of interest, the better — idle traffic before and after helps establish frame boundaries.

### `python3 -m cli.dongle_tool --device /dev/hidraw0 monitor`

Connects to the dongle and prints parsed sensor events as they arrive. Add `--debug` to see raw packet bytes alongside parsed events. Useful for checking whether an unrecognised packet type is arriving (you will see an `unknown:*` event with raw hex payload).

### `python3 -m cli.dongle_tool --device /dev/hidraw0 raw <bytes>`

Sends arbitrary hex bytes to the dongle. **Advanced use only** — sending unknown commands can confuse the dongle and require a power cycle to recover.

```bash
python3 -m cli.dongle_tool --device /dev/hidraw0 raw "aa,55,43,03,04,01,49"
```

---

## Adding a test fixture

When you have a capture that reproduces a bug or exercises new functionality, add it as a test fixture:

1. Copy the `.bin` file to `tests/fixtures/`
2. Open `tests/test_fixtures.py` and add entries to `EXPECTED_EVENTS` for the events your capture contains
3. Run `pytest tests/test_fixtures.py -v` to confirm the fixture parses correctly
4. Include the fixture and updated test in your PR

Fixture files must have real MACs replaced with placeholders.

---

## Opening an issue

When reporting a protocol-related bug, include:

- ws2m version (Docker image tag or `VERSION` in `config.py`)
- Sensor model and hardware version (printed on the sensor label)
- Relevant ws2m log section with `log_level: debug` set — look for `unknown:` lines or the sensor MAC
- HID capture `.bin` if available

See `docs/protocol.md` for the packet format reference.
