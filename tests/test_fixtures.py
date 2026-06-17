"""
Fixture-based regression tests for dongle_protocol.py.

These tests parse real or synthetic HID captures from
tests/fixtures/hid_capture.bin (produced by tools/capture_hid.py) and assert
that the parsed events match expectations.  All tests in this module are
automatically skipped if the fixture file is absent.

A synthetic fixture covering the bridge init handshake and two contact-sensor
events is committed to the repo under tests/fixtures/hid_capture.bin so the
tests run in CI and in the sandbox without real hardware.

To replace the synthetic fixture with a real one:
    sudo python3 tools/capture_hid.py --device /dev/wyzesense
    # trigger sensors, then Ctrl-C or wait for --duration
    # follow the MAC obfuscation prompts before saving

After replacing the fixture update EXPECTED_EVENTS below to match.
"""

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wyzesense2mqtt"))

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "hid_capture.bin")

# ---------------------------------------------------------------------------
# Expected events to assert against when parsing the fixture.
#
# These match the synthetic hid_capture.bin committed to the repo:
#   Frame 4: NOTIFY_SENSOR_ALARM — contact sensor AAAAAAAA open (alarm)
#   Frame 5: NOTIFY_SENSOR_ALARM — contact sensor AAAAAAAA heartbeat (status)
#
# Update these when replacing the fixture with a real capture.
# ---------------------------------------------------------------------------

EXPECTED_EVENTS: list[dict] = [
    {
        "mac": "AAAAAAAA",
        "event": "alarm",
        "sensor_type": "switch",
        "state": "open",
        "battery": 80,
        "signal_strength": -50,
    },
    {
        "mac": "AAAAAAAA",
        "event": "status",
        "sensor_type": "switch",
        "battery": 80,
        "signal_strength": -50,
    },
]


# ---------------------------------------------------------------------------
# Fixture reader
# ---------------------------------------------------------------------------


def _read_fixture(path: str) -> list[bytes]:
    """Read a length-prefixed binary capture file into a list of HID frames."""
    frames = []
    with open(path, "rb") as f:
        while True:
            length_byte = f.read(1)
            if not length_byte:
                break
            (length,) = struct.unpack("B", length_byte)
            frame = f.read(length)
            if len(frame) < length:
                break
            frames.append(frame)
    return frames


def _parse_frames(frames: list[bytes]):
    """Parse HID frames into SensorEvent objects, skipping non-event packets."""
    from dongle_protocol import Packet, SensorEvent

    events = []
    buf = b""
    for frame in frames:
        buf += frame
        start = buf.find(b"\xaa\x55")
        if start == -1:
            buf = b""
            continue
        buf = buf[start:]
        try:
            pkt = Packet.parse(buf)
        except EOFError:
            continue
        if pkt is None:
            buf = buf[2:]
            continue
        buf = buf[pkt.length:]

        if pkt.cmd == Packet.NOTIFY_SENSOR_ALARM and len(pkt.payload) >= 19:
            events.append(SensorEvent.from_packet(pkt.payload))
        elif pkt.cmd == Packet.NOTIFY_SENSOR_ALARM2 and len(pkt.payload) >= 10:
            events.append(SensorEvent.from_packet_v2(pkt.payload))

    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.path.isfile(FIXTURE_PATH),
    reason=f"No HID capture fixture at {FIXTURE_PATH} — run tools/capture_hid.py to generate one",
)


@pytest.fixture(scope="module")
def fixture_frames():
    return _read_fixture(FIXTURE_PATH)


@pytest.fixture(scope="module")
def parsed_events(fixture_frames):
    return _parse_frames(fixture_frames)


# ---------------------------------------------------------------------------
# Tests that run whenever the fixture exists
# ---------------------------------------------------------------------------


def test_fixture_has_frames(fixture_frames):
    """The capture file should contain at least one frame."""
    assert len(fixture_frames) >= 1, "Fixture file exists but is empty"


def test_fixture_frames_parse_without_error(fixture_frames):
    """All frames should parse without raising (non-event frames are silently skipped)."""
    from dongle_protocol import Packet

    for i, frame in enumerate(fixture_frames):
        try:
            pkt = Packet.parse(frame)
        except EOFError:
            pass  # short/incomplete frame — acceptable
        except Exception as exc:
            pytest.fail(f"Frame {i} raised unexpected error: {exc}")


def test_fixture_has_parseable_sensor_events(parsed_events):
    """The fixture should yield at least one sensor event."""
    assert len(parsed_events) >= 1, (
        "No sensor events found in fixture — check fixture content or MAC obfuscation"
    )


def test_fixture_all_events_have_valid_mac(parsed_events):
    """Every parsed event should have a valid 8-character MAC."""
    from sensors import SensorRegistry

    for i, ev in enumerate(parsed_events):
        assert SensorRegistry.is_valid_mac(ev.mac), (
            f"Event[{i}] has invalid MAC {ev.mac!r} — check obfuscation in capture file"
        )


def test_fixture_all_events_have_known_sensor_type(parsed_events):
    """Every parsed event should have a recognised sensor_type."""
    from sensors import SENSOR_TYPES

    for i, ev in enumerate(parsed_events):
        sensor_type = getattr(ev, "sensor_type", None)
        if sensor_type is not None:
            assert sensor_type in SENSOR_TYPES, (
                f"Event[{i}] has unrecognised sensor_type {sensor_type!r}"
            )


# ---------------------------------------------------------------------------
# Parametrized expected-event assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index,expected", list(enumerate(EXPECTED_EVENTS)))
def test_fixture_event_matches_expected(parsed_events, index, expected):
    """Each entry in EXPECTED_EVENTS is checked against the corresponding parsed event."""
    assert index < len(parsed_events), (
        f"EXPECTED_EVENTS[{index}] defined but fixture only has {len(parsed_events)} event(s)"
    )
    ev = parsed_events[index]
    for attr, value in expected.items():
        actual = getattr(ev, attr, None)
        assert actual == value, f"Event[{index}].{attr}: expected {value!r}, got {actual!r}"
