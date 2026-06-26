"""
Tests for dongle_protocol.py — packet framing/parsing and sensor event parsing.

All tests are pure (no USB hardware required).  Byte payloads are constructed
synthetically using the same struct layouts as the production code.
"""

import struct
import time

import pytest

from conftest import (
    make_alarm_payload,
    make_climate_payload,
    make_leak_v2_payload,
    make_packet_bytes,
)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def test_bytes_to_hex_normal():
    from dongle_protocol import bytes_to_hex

    assert bytes_to_hex(b"\xaa\x55\x53") == "aa,55,53"


def test_bytes_to_hex_none():
    from dongle_protocol import bytes_to_hex

    assert bytes_to_hex(None) == "<None>"


def test_bytes_to_hex_empty():
    from dongle_protocol import bytes_to_hex

    assert bytes_to_hex(b"") == "<None>"


# ---------------------------------------------------------------------------
# Packet framing — factory methods produce parseable bytes
# ---------------------------------------------------------------------------


def _round_trip(pkt):
    """Serialise a Packet to bytes via send() then parse back with Packet.parse()."""
    import io
    import os
    import tempfile

    from dongle_protocol import Packet

    # Write to a temp file so we can use os.write / os.read
    with tempfile.TemporaryFile() as f:
        fd = f.fileno()
        pkt.send(fd)
        f.seek(0)
        raw = f.read()

    return Packet.parse(raw), raw


@pytest.mark.parametrize("factory,kwargs", [
    ("get_version", {}),
    ("inquiry", {}),
    ("get_mac", {}),
    ("get_key", {}),
    ("enable_scan", {}),
    ("disable_scan", {}),
    ("get_sensor_count", {}),
    ("finish_auth", {}),
    ("del_all_sensors", {}),
])
def test_packet_round_trip_no_payload(factory, kwargs):
    from dongle_protocol import Packet

    original = getattr(Packet, factory)(**kwargs)
    parsed, _ = _round_trip(original)
    assert parsed is not None
    assert parsed.cmd == original.cmd


def test_packet_round_trip_get_enr():
    from dongle_protocol import Packet

    r = b"\x30" * 16
    pkt = Packet.get_enr(r)
    parsed, _ = _round_trip(pkt)
    assert parsed is not None
    assert parsed.payload == r


def test_packet_round_trip_get_sensor_list():
    from dongle_protocol import Packet

    pkt = Packet.get_sensor_list(5)
    parsed, _ = _round_trip(pkt)
    assert parsed is not None
    assert parsed.payload == b"\x05"


def test_packet_round_trip_del_sensor_ascii():
    from dongle_protocol import Packet

    pkt = Packet.del_sensor("AAAAAAAA")
    parsed, _ = _round_trip(pkt)
    assert parsed is not None
    assert parsed.payload == b"AAAAAAAA"


def test_packet_round_trip_play_chime():
    from dongle_protocol import Packet

    pkt = Packet.play_chime("AAAAAAAA", ring_id=3, repeat_count=2, volume=5)
    parsed, _ = _round_trip(pkt)
    assert parsed is not None
    assert parsed.payload == b"AAAAAAAA" + bytes([3, 2, 5])


def test_packet_play_chime_volume_clamped():
    """Volume is clamped to 1–9."""
    from dongle_protocol import Packet

    pkt_low = Packet.play_chime("AAAAAAAA", ring_id=1, repeat_count=1, volume=0)
    pkt_high = Packet.play_chime("AAAAAAAA", ring_id=1, repeat_count=1, volume=99)
    assert pkt_low.payload[-1] == 1
    assert pkt_high.payload[-1] == 9


def test_packet_length_property():
    from dongle_protocol import Packet

    pkt = Packet.del_sensor("AAAAAAAA")  # 8-byte payload
    # length = payload_len + 7
    assert pkt.length == 8 + 7


# ---------------------------------------------------------------------------
# Packet.parse error cases
# ---------------------------------------------------------------------------


def test_parse_too_short_raises_eoferror():
    from dongle_protocol import Packet

    with pytest.raises(EOFError):
        Packet.parse(b"\x55\xaa\x53")  # only 3 bytes


def test_parse_bad_magic_returns_none():
    from dongle_protocol import Packet

    result = Packet.parse(b"\xDE\xAD\x53\x02\x00\x00\x00")
    assert result is None


def test_parse_bad_checksum_returns_none():
    from dongle_protocol import Packet

    # Build a valid packet then corrupt the checksum
    data = make_packet_bytes(0x43, 0x27, b"")  # CMD_INQUIRY
    corrupted = data[:-2] + bytes([0xFF, 0xFF])
    result = Packet.parse(corrupted)
    assert result is None


def test_parse_truncated_raises_eoferror():
    from dongle_protocol import Packet

    data = make_packet_bytes(0x53, 0x2E, b"")  # CMD_GET_SENSOR_COUNT
    # Remove the last byte so the declared length doesn't match
    with pytest.raises(EOFError):
        Packet.parse(data[:-1])


def test_parse_valid_packet_succeeds():
    from dongle_protocol import Packet

    data = make_packet_bytes(0x43, 0x27, b"")  # CMD_INQUIRY
    pkt = Packet.parse(data)
    assert pkt is not None
    assert pkt.cmd == Packet.CMD_INQUIRY


# ---------------------------------------------------------------------------
# SensorEvent — v1 alarm parsing
# ---------------------------------------------------------------------------


def test_sensor_event_contact_open():
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(
        mac="AAAAAAAA",
        event=0xA2,       # EVENT_ALARM
        sensor_type=0x01, # SENSOR_TYPE_SWITCH
        battery=80,
        state=1,          # open
        signal_strength=50,
        timestamp_ms=1_700_000_000_000,
    )
    ev = SensorEvent.from_packet(payload)

    assert ev.mac == "AAAAAAAA"
    assert ev.event == "alarm"
    assert ev.sensor_type == "switch"
    assert ev.state == "open"
    # raw=80, switch (CR1632 3V): voltage=80/32.0=2.5V; range 2.4–3.2V → ~13%
    assert ev.battery_voltage == pytest.approx(2.5, abs=0.001)
    assert ev.battery == 13
    assert ev.signal_strength == -50  # negated
    assert abs(ev.timestamp - 1_700_000_000.0) < 0.001


def test_sensor_event_contact_closed():
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(mac="AAAAAAAA", sensor_type=0x01, state=0)
    ev = SensorEvent.from_packet(payload)
    assert ev.state == "closed"


def test_sensor_event_motion_active():
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(mac="BBBBBBBB", sensor_type=0x02, state=1)
    ev = SensorEvent.from_packet(payload)
    assert ev.sensor_type == "motion"
    assert ev.state == "active"


def test_sensor_event_motion_inactive():
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(mac="BBBBBBBB", sensor_type=0x02, state=0)
    ev = SensorEvent.from_packet(payload)
    assert ev.state == "inactive"


def test_sensor_event_heartbeat():
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(
        mac="AAAAAAAA",
        event=0xA1,  # EVENT_HEARTBEAT
        sensor_type=0x01,
        battery=65,
        signal_strength=40,
    )
    ev = SensorEvent.from_packet(payload)
    assert ev.event == "status"
    # raw=65, switch (3V): voltage=65/32.0=2.03V; below v_min 2.4V → 0%
    assert ev.battery_voltage == pytest.approx(2.03125, abs=0.001)
    assert ev.battery == 0
    assert ev.signal_strength == -40
    assert not hasattr(ev, "state")


def test_sensor_event_v2_contact_open():
    """V2 contact sensor (switchv2) alarm."""
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(
        mac="CCCCCCCC",
        event=0xA2,
        sensor_type=0x0E,  # SENSOR_TYPE_SWITCH_V2
        battery=50,        # raw; doubled by SensorEvent.__init__ for V2 contact
        state=1,
        signal_strength=35,
    )
    ev = SensorEvent.from_packet(payload)
    assert ev.sensor_type == "switchv2"
    assert ev.state == "open"
    # raw=50, switchv2 (AAA 1.5V, doubled→100): voltage=100/32.0=3.125V; range 1.8–3.2V → ~95%
    assert ev.battery_voltage == pytest.approx(3.125, abs=0.001)
    assert ev.battery == 95


def test_sensor_event_v2_contact_battery_capped_at_100():
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(mac="CCCCCCCC", sensor_type=0x0E, battery=90)
    ev = SensorEvent.from_packet(payload)
    # raw=90, doubled→180: voltage=180/32.0=5.625V; above v_max → capped at 100%
    assert ev.battery == 100


def test_sensor_event_motion_v2():
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(mac="DDDDDDDD", sensor_type=0x0F, state=1)
    ev = SensorEvent.from_packet(payload)
    assert ev.sensor_type == "motionv2"
    assert ev.state == "active"


# ---------------------------------------------------------------------------
# SensorEvent — v1 climate parsing
# ---------------------------------------------------------------------------


def test_sensor_event_climate():
    from dongle_protocol import SensorEvent

    payload = make_climate_payload(
        mac="CCCCCCCC",
        event=0xE8,
        sensor_type=0x07,  # SENSOR_TYPE_CLIMATE
        battery=90,
        temp_hi=22,
        temp_lo=50,
        humidity=55,
        signal_strength=60,
    )
    ev = SensorEvent.from_packet(payload)

    assert ev.mac == "CCCCCCCC"
    assert ev.event == "status"
    assert ev.sensor_type == "climate"
    assert ev.temperature == "22.50"
    assert ev.humidity == 55
    # raw=90, climate (CR2450 3V): voltage=90/32.0=2.8125V; range 2.4–3.2V → ~52%
    assert ev.battery_voltage == pytest.approx(2.8125, abs=0.001)
    assert ev.battery == 52
    assert ev.signal_strength == -60  # now correctly from offset 9


def test_sensor_event_climate_temp_decimal():
    from dongle_protocol import SensorEvent

    payload = make_climate_payload(temp_hi=18, temp_lo=75)
    ev = SensorEvent.from_packet(payload)
    assert ev.temperature == "18.75"


# ---------------------------------------------------------------------------
# SensorEvent — v2 leak parsing
# ---------------------------------------------------------------------------


def test_sensor_event_leak_dry():
    from dongle_protocol import SensorEvent

    payload = make_leak_v2_payload(
        mac="DDDDDDDD",
        event=0xEA,
        sensor_type=0x03,
        battery=75,
        state=0,        # dry
        probe_state=0,
        probe_available=1,
        signal_strength=45,
    )
    ev = SensorEvent.from_packet_v2(payload)

    assert ev.mac == "DDDDDDDD"
    assert ev.event == "alarm"
    assert ev.sensor_type == "leak"
    assert ev.state == "dry"
    assert ev.probe_state == "dry"
    assert ev.probe_available is True
    # raw=75, leak (CR2450 3V): voltage=75/32.0=2.34375V; below v_min 2.4V → 0%
    assert ev.battery_voltage == pytest.approx(2.34375, abs=0.001)
    assert ev.battery == 0
    assert ev.signal_strength == -45


def test_sensor_event_leak_wet():
    from dongle_protocol import SensorEvent

    payload = make_leak_v2_payload(state=1, probe_state=1)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.state == "wet"
    assert ev.probe_state == "wet"


def test_sensor_event_leak_probe_not_available():
    from dongle_protocol import SensorEvent

    payload = make_leak_v2_payload(probe_available=0)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.probe_available is False


# ---------------------------------------------------------------------------
# SensorEvent — unknown event type falls back gracefully
# ---------------------------------------------------------------------------


def test_sensor_event_unknown_type_does_not_raise():
    from dongle_protocol import SensorEvent

    # Use a valid alarm payload layout but with an unknown sensor_type byte
    payload = make_alarm_payload(mac="AAAAAAAA", event=0xA2, sensor_type=0xFF)
    ev = SensorEvent.from_packet(payload)
    # Should not raise; produces an "unknown:A2" event with raw field
    assert "unknown" in ev.event
    assert hasattr(ev, "raw")


def test_sensor_event_unknown_event_byte():
    from dongle_protocol import SensorEvent

    # Build a packet with an event byte not in the known set (0xA1/0xA2/0xE8)
    payload = make_alarm_payload(mac="AAAAAAAA", event=0xBB, sensor_type=0x01)
    ev = SensorEvent.from_packet(payload)
    assert "unknown" in ev.event


# ---------------------------------------------------------------------------
# Packet factory methods not yet covered
# ---------------------------------------------------------------------------


def test_packet_get_sensor_r1():
    from dongle_protocol import Packet

    r1 = b"Ok5HPNQ4lf77u754"
    pkt = Packet.get_sensor_r1("AAAAAAAA", r1)
    assert isinstance(pkt, Packet)
    assert pkt.payload == b"AAAAAAAA" + r1


def test_packet_verify_sensor():
    from dongle_protocol import Packet

    pkt = Packet.verify_sensor("AAAAAAAA")
    assert isinstance(pkt, Packet)
    assert pkt.payload[:8] == b"AAAAAAAA"


def test_packet_sync_time_ack():
    from dongle_protocol import Packet

    pkt = Packet.sync_time_ack()
    assert isinstance(pkt, Packet)
    assert len(pkt.payload) == 8  # struct.pack(">Q", ...)


def test_packet_async_ack():
    from dongle_protocol import Packet

    pkt = Packet.async_ack(Packet.CMD_GET_DONGLE_VERSION)
    assert pkt.cmd == Packet.ASYNC_ACK
    # payload is an int for ACK packets
    assert isinstance(pkt.payload, int)


def test_packet_str_ack():
    from dongle_protocol import Packet

    pkt = Packet.async_ack(Packet.CMD_GET_DONGLE_VERSION)
    s = str(pkt)
    assert "ACK" in s


def test_packet_str_normal():
    from dongle_protocol import Packet

    pkt = Packet.get_sensor_count()
    s = str(pkt)
    assert "Cmd=" in s
    assert "Payload=" in s


def test_packet_del_sensor_bytes_variant():
    from dongle_protocol import Packet

    mac_bytes = b"AAAAAAAA"
    pkt = Packet.del_sensor(mac_bytes)
    assert pkt.payload == mac_bytes


# ---------------------------------------------------------------------------
# Packet.parse — ASYNC_ACK path
# ---------------------------------------------------------------------------


def test_parse_async_ack_packet():
    """Verify that an ASYNC_ACK packet round-trips through parse correctly."""
    import struct
    from dongle_protocol import Packet, _TYPE_ASYNC, _checksum

    # Build a valid ASYNC_ACK frame manually
    # Header: AA 55 | cmd_type=53 | b2=FF | cmd_id=FF | checksum
    cmd_type = _TYPE_ASYNC
    cmd_id_ack = 0xFF
    b2 = Packet.CMD_GET_DONGLE_VERSION & 0xFF  # inner cmd byte

    pkt_bytes = struct.pack(">HB", 0xAA55, cmd_type)
    pkt_bytes += struct.pack("BB", b2, cmd_id_ack)
    pkt_bytes += struct.pack(">H", 0)  # placeholder checksum
    # Recompute checksum
    cs = _checksum(pkt_bytes[:-2])
    pkt_bytes = pkt_bytes[:-2] + struct.pack(">H", cs)

    parsed = Packet.parse(pkt_bytes)
    assert parsed is not None
    assert parsed.cmd == Packet.ASYNC_ACK


# ---------------------------------------------------------------------------
# SensorEvent.__str__
# ---------------------------------------------------------------------------


def test_sensor_event_str_contains_key_fields():
    from dongle_protocol import SensorEvent

    ev = SensorEvent("alarm", "AAAAAAAA", 1700000000.0, sensor_type="switch", state="open", battery=80)
    s = str(ev)
    assert "alarm" in s
    assert "AAAAAAAA" in s
    assert "open" in s
    assert "battery" in s


# ---------------------------------------------------------------------------
# Non-ASCII MAC decode paths
# ---------------------------------------------------------------------------


def test_sensor_event_non_ascii_mac_in_alarm():
    """Non-ASCII MAC bytes in a v1 packet are decoded via latin-1 rather than
    raising UnicodeDecodeError.  The dongle should never produce such MACs in
    normal operation, but the bridge should not crash if it encounters one.
    """
    import struct
    from dongle_protocol import SensorEvent

    mac_bytes = b"\x80\x81\x82\x83\x84\x85\x86\x87"
    timestamp_ms = 1_700_000_000_000
    header = struct.pack(">QB8sB", timestamp_ms, 0xA2, mac_bytes, 0x01)
    body = struct.pack(">BBBBBHB", 0, 50, 0, 0, 0, 1, 30)
    payload = header + body

    # Should not raise; MAC is decoded as latin-1 and falls through to _parse_unknown
    ev = SensorEvent.from_packet(payload)
    assert ev.mac == mac_bytes.decode("latin-1")


# ---------------------------------------------------------------------------
# Keypad HMS event parsing
# ---------------------------------------------------------------------------


def test_keypad_mode_disarmed():
    """Keypad mode event: state_byte=0x01 → alarm_mode='disarmed'."""
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x02, state_byte=0x01, battery=100, signal_strength=40)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.event == "keypad_mode"
    assert ev.sensor_type == "keypad"
    assert ev.alarm_mode == "disarmed"
    assert ev.signal_strength == -40


def test_keypad_mode_armed_home():
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x02, state_byte=0x02)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.alarm_mode == "armed_home"


def test_keypad_mode_armed_away():
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x02, state_byte=0x03)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.alarm_mode == "armed_away"


def test_keypad_mode_triggered():
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x02, state_byte=0x04)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.alarm_mode == "triggered"


def test_keypad_mode_inactive_is_unknown():
    """State byte 0x00 (Inactive/transient) maps to 'unknown'."""
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x02, state_byte=0x00)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.alarm_mode == "unknown"


def test_keypad_mode_unknown_state():
    """Unknown state byte is represented as 'unknown:NN' rather than raising."""
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x02, state_byte=0xFF)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.alarm_mode.startswith("unknown:")


def test_keypad_motion_active():
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x0A, state_byte=1)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.event == "keypad_motion"
    assert ev.motion == "active"


def test_keypad_motion_inactive():
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x0A, state_byte=0)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.motion == "inactive"


def test_keypad_pin_start():
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x06)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.event == "keypad_pin_start"
    assert ev.sensor_type == "keypad"
    assert not hasattr(ev, "pin")


def test_keypad_pin_confirm_with_pin():
    """PIN confirm event carries PIN digits."""
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x08, pin="1234")
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.event == "keypad_pin_confirm"
    assert ev.pin == "1234"


def test_keypad_pin_confirm_empty_pin():
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x08, pin="")
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.event == "keypad_pin_confirm"
    assert ev.pin == ""


def test_keypad_battery_normalised():
    """Raw battery value 155 → 100%; 78 → ~50%."""
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload_full = make_keypad_hms_payload(sub_event=0x02, state_byte=1, battery=155)
    ev_full = SensorEvent.from_packet_v2(payload_full)
    assert ev_full.battery == 100

    payload_half = make_keypad_hms_payload(sub_event=0x02, state_byte=1, battery=78)
    ev_half = SensorEvent.from_packet_v2(payload_half)
    assert ev_half.battery == 50


def test_keypad_short_payload_returns_unknown():
    """A payload too short to parse produces an 'unknown:*' event rather than crashing."""
    import struct
    from dongle_protocol import SensorEvent, SENSOR_TYPE_KEYPAD

    # Only the 10-byte header, no data bytes at all
    header = struct.pack(">B8sB", 0x00, b"KPADKPAD", SENSOR_TYPE_KEYPAD)
    # from_packet_v2 strips 10 bytes, leaving 0 bytes of data
    ev = SensorEvent.from_packet_v2(header)
    assert ev.event.startswith("unknown:")


def test_keypad_unknown_sub_event():
    """Unrecognised sub-event byte produces an 'unknown:*' event."""
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0xFF)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.event.startswith("unknown:")


def test_keypad_sensor_type_name():
    """SENSOR_TYPE_KEYPAD (0x05) maps to 'keypad' in SENSOR_TYPE_NAMES."""
    from dongle_protocol import SENSOR_TYPE_KEYPAD, SENSOR_TYPE_NAMES

    assert SENSOR_TYPE_NAMES[SENSOR_TYPE_KEYPAD] == "keypad"


def test_keypad_alarm_sub_event():
    """Sub-event 0x0C produces a 'keypad_alarm' event with alarm_raw field."""
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x0C, state_byte=0x01)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.event == "keypad_alarm"
    assert ev.sensor_type == "keypad"
    assert ev.alarm_raw == 0x01


def test_send_keypad_status_packet_builds():
    """Packet.send_keypad_status builds a CMD_SEND_KEYPAD_EVENT packet."""
    from dongle_protocol import Packet

    pkt = Packet.send_keypad_status("KPADKPAD", 0x01)
    raw = pkt._to_bytes() if hasattr(pkt, "_to_bytes") else None
    # Verify the cmd encodes CMD_SEND_KEYPAD_EVENT (0x53/0x53)
    assert pkt.cmd == Packet.CMD_SEND_KEYPAD_EVENT
    # State byte 0x01 (disarmed) should be in the payload
    assert 0x01 in pkt.payload


def test_send_keypad_status_all_states():
    """send_keypad_status accepts all known state bytes without raising."""
    from dongle_protocol import Packet

    for state_byte in (0x01, 0x02, 0x03, 0x04, 0x05):
        pkt = Packet.send_keypad_status("KPADKPAD", state_byte)
        assert pkt.cmd == Packet.CMD_SEND_KEYPAD_EVENT


# ---------------------------------------------------------------------------
# die_temp — new field extracted from alarm/heartbeat/climate offset 0
# ---------------------------------------------------------------------------


def test_alarm_event_has_die_temp():
    """die_temp is extracted from offset 0 of alarm event data."""
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(sensor_type=0x01, die_temp=25)
    ev = SensorEvent.from_packet(payload)
    assert ev.die_temp == 25


def test_heartbeat_event_has_die_temp():
    """die_temp is present on heartbeat (status) events."""
    from dongle_protocol import SensorEvent

    payload = make_alarm_payload(event=0xA1, sensor_type=0x01, die_temp=18)
    ev = SensorEvent.from_packet(payload)
    assert ev.event == "status"
    assert ev.die_temp == 18


def test_climate_event_has_die_temp():
    """die_temp is extracted from offset 0 of climate event data."""
    from dongle_protocol import SensorEvent

    payload = make_climate_payload(die_temp=22)
    ev = SensorEvent.from_packet(payload)
    assert ev.die_temp == 22


# ---------------------------------------------------------------------------
# battery_voltage — AON_BATMON raw/32.0 conversion
# ---------------------------------------------------------------------------


def test_battery_voltage_none_for_keypad():
    """Keypad uses a different battery scale; battery_voltage is None."""
    from conftest import make_keypad_hms_payload
    from dongle_protocol import SensorEvent

    payload = make_keypad_hms_payload(sub_event=0x02, state_byte=0x01, battery=155)
    ev = SensorEvent.from_packet_v2(payload)
    assert ev.battery_voltage is None
    assert ev.battery == 100


def test_battery_voltage_none_for_chime_type():
    """Chime has no battery_v_min/max; battery_voltage is set, battery is None."""
    from dongle_protocol import SensorEvent

    # Construct minimal event directly — chime doesn't send alarm packets normally
    ev = SensorEvent("status", "CHIMCHIM", 0.0, sensor_type="chime", battery_raw=64)
    assert ev.battery_voltage == pytest.approx(2.0, abs=0.001)
    assert ev.battery is None


def test_battery_voltage_below_min_gives_zero_percent():
    """Voltage below v_min clamps to 0%."""
    from dongle_protocol import SensorEvent

    # raw=70, switch: voltage=70/32.0=2.1875V; below v_min=2.4V → 0%
    payload = make_alarm_payload(sensor_type=0x01, battery=70)
    ev = SensorEvent.from_packet(payload)
    assert ev.battery_voltage == pytest.approx(2.1875, abs=0.001)
    assert ev.battery == 0


def test_battery_voltage_above_max_gives_100_percent():
    """Voltage above v_max clamps to 100%."""
    from dongle_protocol import SensorEvent

    # raw=104, switch: voltage=104/32.0=3.25V; above v_max=3.2V → 100%
    payload = make_alarm_payload(sensor_type=0x01, battery=104)
    ev = SensorEvent.from_packet(payload)
    assert ev.battery_voltage == pytest.approx(3.25, abs=0.001)
    assert ev.battery == 100


# ---------------------------------------------------------------------------
# mqtt.py — new diagnostic entities and probe gating
# ---------------------------------------------------------------------------


def test_diagnostic_components_include_battery_voltage():
    """battery_voltage entity is present in diagnostic components."""
    import mqtt as mqtt_module

    components = mqtt_module._build_diagnostic_components()
    assert "battery_voltage" in components
    assert components["battery_voltage"]["device_class"] == "voltage"
    assert components["battery_voltage"]["unit_of_measurement"] == "V"


def test_diagnostic_components_include_die_temp():
    """die_temp entity is present in diagnostic components and disabled by default."""
    import mqtt as mqtt_module

    components = mqtt_module._build_diagnostic_components()
    assert "die_temp" in components
    assert components["die_temp"]["device_class"] == "temperature"
    assert components["die_temp"]["enabled_by_default"] is False


def test_leak_components_include_probe_state_when_probe_available():
    """probe_state entity is included when probe_available=True (default)."""
    import mqtt as mqtt_module

    cfg = {"self_topic_root": "wyzesense2mqtt", "hass_topic_root": "homeassistant"}
    components = mqtt_module._build_leak_sensor_components("DDDDDDDD", {}, "wyzesense2mqtt/DDDDDDDD", probe_available=True)
    assert "probe_state" in components


def test_leak_components_exclude_probe_state_when_no_probe():
    """probe_state entity is omitted when probe_available=False."""
    import mqtt as mqtt_module

    components = mqtt_module._build_leak_sensor_components("DDDDDDDD", {}, "wyzesense2mqtt/DDDDDDDD", probe_available=False)
    assert "probe_state" not in components
    assert "state" in components
    assert "temperature" in components
    assert "humidity" in components
