"""
WyzeSense USB dongle protocol library.

Handles low-level communication with the Wyze Sense USB bridge dongle
(HID device, vendor 1a86 / product e024).  This module was originally
derived from HclX/WyzeSensePy and has since diverged significantly.

Public API
----------
open_dongle(device, event_handler, logger) -> Dongle
    Open the dongle at *device* path and return a connected Dongle instance.

Dongle
    .mac         – dongle MAC address string
    .version     – firmware version string
    .enr         – ENR bytes
    .list()      – list paired sensor MACs
    .scan()      – pair a new sensor (blocks up to 60 s)
    .delete(mac) – remove a paired sensor
    .delete_all()
    .play_chime(mac, ring_id, repeat_count, volume)
    .send_raw(data)
    .check_error()
    .stop()
"""

import datetime
import logging
import os
import struct
import threading
import time

# ---------------------------------------------------------------------------
# Default logger; overridden when open_dongle() is called with a logger argument.
# ---------------------------------------------------------------------------

_logger = logging.getLogger("ws2m.dongle")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def bytes_to_hex(data: bytes | None) -> str:
    """Format *data* as a comma-separated hex string, e.g. ``'aa,55,53'``."""
    if data:
        return ",".join(f"{b:02x}" for b in data)
    return "<None>"


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFFFF


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

_TYPE_SYNC = 0x43
_TYPE_ASYNC = 0x53


def _make_cmd(cmd_type: int, cmd_id: int) -> int:
    return (cmd_type << 8) | cmd_id


# Numeric sensor type IDs as reported by the dongle
SENSOR_TYPE_SWITCH = 0x01
SENSOR_TYPE_MOTION = 0x02
SENSOR_TYPE_LEAK = 0x03
SENSOR_TYPE_KEYPAD = 0x05
SENSOR_TYPE_CLIMATE = 0x07
SENSOR_TYPE_CHIME = 0x0C
SENSOR_TYPE_SWITCH_V2 = 0x0E
SENSOR_TYPE_MOTION_V2 = 0x0F

# Map numeric sensor type → string name used throughout the rest of the codebase
SENSOR_TYPE_NAMES: dict[int, str] = {
    SENSOR_TYPE_SWITCH: "switch",
    SENSOR_TYPE_SWITCH_V2: "switchv2",
    SENSOR_TYPE_MOTION: "motion",
    SENSOR_TYPE_MOTION_V2: "motionv2",
    SENSOR_TYPE_LEAK: "leak",
    SENSOR_TYPE_CLIMATE: "climate",
    SENSOR_TYPE_CHIME: "chime",
    SENSOR_TYPE_KEYPAD: "keypad",
}

# Map numeric sensor type → (off_state, on_state) string tuple
_BINARY_SENSOR_STATES: dict[int, tuple[str, str]] = {
    SENSOR_TYPE_SWITCH: ("closed", "open"),
    SENSOR_TYPE_SWITCH_V2: ("closed", "open"),
    SENSOR_TYPE_MOTION: ("inactive", "active"),
    SENSOR_TYPE_MOTION_V2: ("inactive", "active"),
    SENSOR_TYPE_LEAK: ("dry", "wet"),
}

# Event type bytes in the HID packet stream
_EVENT_HEARTBEAT = 0xA1
_EVENT_ALARM = 0xA2
_EVENT_CLIMATE = 0xE8
_EVENT_LEAK = 0xEA

# Keypad sub-event type bytes (found inside the NOTIFY_SENSOR_ALARM2 HMS payload)
_KEYPAD_EVENT_MODE = 0x02  # arm/disarm state change request
_KEYPAD_EVENT_MOTION = 0x0A  # PIR motion sensor
_KEYPAD_EVENT_PIN_START = 0x06  # user started entering a PIN (no digits yet); keypad may be polling for status
_KEYPAD_EVENT_PIN_CONFIRM = 0x08  # user finished entering a PIN (digits follow)
_KEYPAD_EVENT_ALARM = 0x0C  # some kind of alarm event (exact meaning TBD)

# Keypad mode state values — raw byte from event_data[6].
# Both the PR (drinfernoo) and AK5nowman/WyzeSense agree on this mapping:
#   PR:        states[raw] = ["unknown","disarmed","armed_home","armed_away","triggered"]
#   AK5nowman: (WyzeKeyPadState)(raw + 1), where enum is {1=Active,2=Disarmed,3=Home,4=Away,5=Alarm}
# Both resolve to the same raw→meaning mapping.
_KEYPAD_MODE_STATES: dict[int, str] = {
    0x00: "unknown",  # Inactive / transient — no stable alarm panel meaning
    0x01: "disarmed",
    0x02: "armed_home",
    0x03: "armed_away",
    0x04: "triggered",
}


# ---------------------------------------------------------------------------
# Packet – encodes/decodes the HID wire format
# ---------------------------------------------------------------------------


class Packet:
    """Encapsulates a single WyzeSense USB protocol packet."""

    _DEFAULT_TIMEOUT = 5  # seconds

    # Sync commands (host-initiated)
    CMD_GET_ENR = _make_cmd(_TYPE_SYNC, 0x02)
    CMD_GET_MAC = _make_cmd(_TYPE_SYNC, 0x04)
    CMD_GET_KEY = _make_cmd(_TYPE_SYNC, 0x06)
    CMD_INQUIRY = _make_cmd(_TYPE_SYNC, 0x27)
    CMD_UPDATE_CC1310 = _make_cmd(_TYPE_SYNC, 0x12)
    CMD_SET_CH554_UPGRADE = _make_cmd(_TYPE_SYNC, 0x0E)

    # Async commands / notifications
    ASYNC_ACK = _make_cmd(_TYPE_ASYNC, 0xFF)
    CMD_FINISH_AUTH = _make_cmd(_TYPE_ASYNC, 0x14)
    CMD_GET_DONGLE_VERSION = _make_cmd(_TYPE_ASYNC, 0x16)
    CMD_START_STOP_SCAN = _make_cmd(_TYPE_ASYNC, 0x1C)
    CMD_GET_SENSOR_R1 = _make_cmd(_TYPE_ASYNC, 0x21)
    CMD_VERIFY_SENSOR = _make_cmd(_TYPE_ASYNC, 0x23)
    CMD_DEL_SENSOR = _make_cmd(_TYPE_ASYNC, 0x25)
    CMD_DEL_ALL_SENSORS = _make_cmd(_TYPE_ASYNC, 0x3F)
    CMD_GET_SENSOR_COUNT = _make_cmd(_TYPE_ASYNC, 0x2E)
    CMD_GET_SENSOR_LIST = _make_cmd(_TYPE_ASYNC, 0x30)
    CMD_PLAY_CHIME = _make_cmd(_TYPE_ASYNC, 0x70)
    # Send the current alarm state to the keypad (drives its display/LEDs).
    # cmd_type=0x53 (ASYNC), cmd_id=0x53.  Payload layout confirmed from
    # AK5nowman/WyzeSense C# (KeyPadEventPacket): [0xAA][0x55][0x53][0x0F][0x53]
    # [0x00]*8 [state_byte] [0x00][cs_hi][cs_lo].  State byte values:
    #   0x01=disarmed, 0x02=armed_home, 0x03=armed_away, 0x04=inactive(?), 0x05=triggered
    # NOTE: the outer framing is handled by Packet.send(); only the inner
    # payload bytes (after cmd_id) are stored here.
    CMD_SEND_KEYPAD_EVENT = _make_cmd(_TYPE_ASYNC, 0x53)

    NOTIFY_SENSOR_ALARM = _make_cmd(_TYPE_ASYNC, 0x19)
    NOTIFY_SENSOR_SCAN = _make_cmd(_TYPE_ASYNC, 0x20)
    NOTIFY_SYNC_TIME = _make_cmd(_TYPE_ASYNC, 0x32)
    NOTIFY_EVENT_LOG = _make_cmd(_TYPE_ASYNC, 0x35)
    NOTIFY_SENSOR_ALARM2 = _make_cmd(_TYPE_ASYNC, 0x55)

    def __init__(self, cmd: int, payload=b""):
        self._cmd = cmd
        if self._cmd == self.ASYNC_ACK:
            assert isinstance(payload, int)
        else:
            assert isinstance(payload, bytes)
        self._payload = payload

    def __str__(self) -> str:
        if self._cmd == self.ASYNC_ACK:
            return f"Packet: Cmd={self._cmd:04X}, Payload=ACK({self._payload:04X})"
        return f"Packet: Cmd={self._cmd:04X}, Payload={bytes_to_hex(self._payload)}"

    @property
    def length(self) -> int:
        return 7 if self._cmd == self.ASYNC_ACK else len(self._payload) + 7

    @property
    def cmd(self) -> int:
        return self._cmd

    @property
    def payload(self) -> bytes:
        return self._payload

    def send(self, fd: int) -> None:
        pkt = struct.pack(">HB", 0xAA55, self._cmd >> 8)
        if self._cmd == self.ASYNC_ACK:
            pkt += struct.pack("BB", self._payload & 0xFF, self._cmd & 0xFF)
        else:
            pkt += struct.pack("BB", len(self._payload) + 3, self._cmd & 0xFF)
            if self._payload:
                pkt += self._payload
        pkt += struct.pack(">H", _checksum(pkt))
        _logger.debug("Sending: %s", bytes_to_hex(pkt))
        written = os.write(fd, pkt)
        assert written == len(pkt)

    @classmethod
    def parse(cls, data: bytes) -> "Packet | None":
        """Parse a packet from *data*.

        Returns None for packets with invalid magic or checksum.  Raises
        EOFError when *data* is too short to contain a complete packet;
        the caller should buffer more data and retry.
        """
        assert isinstance(data, bytes)

        if len(data) < 5:
            _logger.error("Packet too short (%d bytes): %s", len(data), bytes_to_hex(data))
            raise EOFError

        magic, cmd_type, b2, cmd_id = struct.unpack_from(">HBBB", data)
        if magic not in (0x55AA, 0xAA55):
            _logger.error("Invalid packet magic %04X: %s", magic, bytes_to_hex(data))
            return None

        cmd = _make_cmd(cmd_type, cmd_id)
        if cmd == cls.ASYNC_ACK:
            assert len(data) >= 7
            data = data[:7]
            payload = _make_cmd(cmd_type, b2)
        elif len(data) >= b2 + 4:
            data = data[: b2 + 4]
            payload = data[5:-2]
        else:
            _logger.error("Short packet (expected %d, got %d): %s", b2 + 4, len(data), bytes_to_hex(data))
            raise EOFError

        cs_remote = (data[-2] << 8) | data[-1]
        cs_local = _checksum(data[:-2])
        if cs_remote != cs_local:
            _logger.error("Checksum mismatch (remote=%04X, local=%04X): %s", cs_remote, cs_local, bytes_to_hex(data))
            return None

        return cls(cmd, payload)

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def get_version(cls) -> "Packet":
        return cls(cls.CMD_GET_DONGLE_VERSION)

    @classmethod
    def inquiry(cls) -> "Packet":
        return cls(cls.CMD_INQUIRY)

    @classmethod
    def get_enr(cls, r: bytes) -> "Packet":
        assert isinstance(r, bytes) and len(r) == 16
        return cls(cls.CMD_GET_ENR, r)

    @classmethod
    def get_mac(cls) -> "Packet":
        return cls(cls.CMD_GET_MAC)

    @classmethod
    def get_key(cls) -> "Packet":
        return cls(cls.CMD_GET_KEY)

    @classmethod
    def enable_scan(cls) -> "Packet":
        return cls(cls.CMD_START_STOP_SCAN, b"\x01")

    @classmethod
    def disable_scan(cls) -> "Packet":
        return cls(cls.CMD_START_STOP_SCAN, b"\x00")

    @classmethod
    def get_sensor_count(cls) -> "Packet":
        return cls(cls.CMD_GET_SENSOR_COUNT)

    @classmethod
    def get_sensor_list(cls, count: int) -> "Packet":
        assert count <= 0xFF
        return cls(cls.CMD_GET_SENSOR_LIST, struct.pack("B", count))

    @classmethod
    def finish_auth(cls) -> "Packet":
        return cls(cls.CMD_FINISH_AUTH, b"\xff")

    @classmethod
    def del_sensor(cls, mac: str | bytes) -> "Packet":
        if isinstance(mac, bytes):
            assert len(mac) == 8
            return cls(cls.CMD_DEL_SENSOR, mac)
        assert isinstance(mac, str) and len(mac) == 8
        try:
            return cls(cls.CMD_DEL_SENSOR, mac.encode("ascii"))
        except UnicodeEncodeError:
            return cls(cls.CMD_DEL_SENSOR, mac.encode("latin-1"))

    @classmethod
    def del_all_sensors(cls) -> "Packet":
        return cls(cls.CMD_DEL_ALL_SENSORS)

    @classmethod
    def get_sensor_r1(cls, mac: str, r: bytes) -> "Packet":
        assert isinstance(r, bytes) and len(r) == 16
        assert isinstance(mac, str) and len(mac) == 8
        return cls(cls.CMD_GET_SENSOR_R1, mac.encode("ascii") + r)

    @classmethod
    def verify_sensor(cls, mac: str) -> "Packet":
        assert isinstance(mac, str) and len(mac) == 8
        return cls(cls.CMD_VERIFY_SENSOR, mac.encode("ascii") + b"\xff\x04")

    @classmethod
    def play_chime(cls, mac: str, ring_id: int, repeat_count: int, volume: int) -> "Packet":
        assert isinstance(mac, str) and len(mac) == 8
        assert isinstance(ring_id, int) and 0 <= ring_id <= 0xFF
        assert isinstance(repeat_count, int) and isinstance(volume, int)
        volume = max(1, min(9, volume))
        return cls(cls.CMD_PLAY_CHIME, mac.encode("ascii") + bytes([ring_id, repeat_count, volume]))

    @classmethod
    def send_keypad_status(cls, mac: str, state_byte: int) -> "Packet":
        """Build a packet that pushes the current alarm state to the keypad display.

        Payload layout confirmed from AK5nowman/WyzeSense C# (KeyPadEventPacket).
        State byte raw values (confirmed by both the PR and AK5nowman):
          0x01 = disarmed
          0x02 = armed_home
          0x03 = armed_away
          0x04 = triggered/alarm
        (Raw byte 0x00 = Inactive/transient; not sent as a status update.)
        """
        assert isinstance(mac, str) and len(mac) == 8
        assert isinstance(state_byte, int) and 0x00 <= state_byte <= 0xFF
        # The C# KeyPadEventPacket encodes these fixed bytes followed by
        # 8 zero bytes, the state, and a trailing zero.
        inner = bytes([0xAA, 0x55, 0x53, 0x0F, 0x53]) + b"\x00" * 8 + bytes([state_byte, 0x00])
        return cls(cls.CMD_SEND_KEYPAD_EVENT, inner)

    @classmethod
    def sync_time_ack(cls) -> "Packet":
        return cls(cls.NOTIFY_SYNC_TIME + 1, struct.pack(">Q", int(time.time() * 1000)))

    @classmethod
    def async_ack(cls, cmd: int) -> "Packet":
        assert (cmd >> 8) == _TYPE_ASYNC
        return cls(cls.ASYNC_ACK, cmd)


# ---------------------------------------------------------------------------
# SensorEvent – parsed sensor data emitted to the event callback
# ---------------------------------------------------------------------------


class SensorEvent:
    """A single event (alarm, heartbeat, or climate reading) from a sensor."""

    def __init__(self, event: str, mac: str, timestamp: float, **kwargs):
        self.__dict__.update(kwargs)

        if "battery" in self.__dict__:
            # V2 contact sensors use a single 1.5 V cell and report half the
            # mV of 3 V sensors; double the raw value to normalise.
            if self.sensor_type == SENSOR_TYPE_NAMES.get(SENSOR_TYPE_SWITCH_V2):
                self.battery = self.battery * 2
            # Keypad reports raw battery in a 0–155 range; normalise to 0–100 %.
            elif self.sensor_type == SENSOR_TYPE_NAMES.get(SENSOR_TYPE_KEYPAD):
                self.battery = int(self.battery / 155 * 100)
            self.battery = min(self.battery, 100)

        if "signal_strength" in self.__dict__:
            # The dongle reports RSSI as a positive integer; negate for dBm
            self.signal_strength = -self.signal_strength

        self.event = event
        self.mac = mac
        self.timestamp = timestamp

    def __str__(self) -> str:
        return ",".join(f"{k}={v}" for k, v in self.__dict__.items())

    # ------------------------------------------------------------------
    # Packet parsers
    # ------------------------------------------------------------------

    @classmethod
    def _parse_alarm(cls, mac: str, event: int, sensor_type: int, timestamp: float, data: bytes) -> "SensorEvent":
        _, battery, _, _, state, _seq, signal_strength = struct.unpack_from(">BBBBBHB", data)
        if sensor_type not in SENSOR_TYPE_NAMES:
            _logger.warning("Unknown sensor type in alarm: 0x%02X", sensor_type)
            return cls._parse_unknown(mac, event, sensor_type, timestamp, data)
        if sensor_type not in _BINARY_SENSOR_STATES:
            _logger.warning("Unexpected sensor type 0x%02X for alarm event 0x%02X", sensor_type, event)
            return cls._parse_unknown(mac, event, sensor_type, timestamp, data)
        return cls(
            "alarm",
            mac,
            timestamp,
            sensor_type=SENSOR_TYPE_NAMES[sensor_type],
            battery=battery,
            signal_strength=signal_strength,
            state=_BINARY_SENSOR_STATES[sensor_type][state],
        )

    @classmethod
    def _parse_heartbeat(cls, mac: str, event: int, sensor_type: int, timestamp: float, data: bytes) -> "SensorEvent":
        _, battery, _, _, _state, _seq, signal_strength = struct.unpack_from(">BBBBBHB", data)
        if sensor_type not in SENSOR_TYPE_NAMES:
            _logger.warning("Unknown sensor type in heartbeat: 0x%02X", sensor_type)
            return cls._parse_unknown(mac, event, sensor_type, timestamp, data)
        return cls(
            "status",
            mac,
            timestamp,
            sensor_type=SENSOR_TYPE_NAMES[sensor_type],
            battery=battery,
            signal_strength=signal_strength,
        )

    @classmethod
    def _parse_climate(cls, mac: str, event: int, sensor_type: int, timestamp: float, data: bytes) -> "SensorEvent":
        _, battery, _, _, temp_hi, temp_lo, humidity, signal_strength = struct.unpack_from(">BBBBBBBB", data)
        if sensor_type not in (SENSOR_TYPE_CLIMATE, SENSOR_TYPE_LEAK):
            _logger.warning("Unexpected sensor type 0x%02X for climate event 0x%02X", sensor_type, event)
            return cls._parse_unknown(mac, event, sensor_type, timestamp, data)
        temperature = f"{temp_hi + (temp_lo / 100.0):.2f}"
        return cls(
            "status",
            mac,
            timestamp,
            sensor_type=SENSOR_TYPE_NAMES[sensor_type],
            battery=battery,
            signal_strength=signal_strength,
            temperature=temperature,
            humidity=humidity,
        )

    @classmethod
    def _parse_leak(cls, mac: str, event: int, sensor_type: int, timestamp: float, data: bytes) -> "SensorEvent":
        _, _, battery, _, _, state, probe_state, probe_available, _, _seq, signal_strength = struct.unpack_from(
            ">BBBBBBBBBBB", data
        )
        if sensor_type not in _BINARY_SENSOR_STATES:
            _logger.warning("Unexpected sensor type 0x%02X for leak event 0x%02X", sensor_type, event)
            return cls._parse_unknown(mac, event, sensor_type, timestamp, data)
        return cls(
            "alarm",
            mac,
            timestamp,
            sensor_type=SENSOR_TYPE_NAMES[sensor_type],
            battery=battery,
            signal_strength=signal_strength,
            state=_BINARY_SENSOR_STATES[sensor_type][state],
            probe_state=_BINARY_SENSOR_STATES[sensor_type][probe_state],
            probe_available=bool(probe_available),
        )

    @classmethod
    def _parse_keypad(cls, mac: str, event: int, sensor_type: int, timestamp: float, data: bytes) -> "SensorEvent":
        """Parse an HMS keypad packet payload.

        Packet layout (data bytes after the 10-byte header stripped by from_packet_v2):
          [0]      total payload length (including this byte)
          [1..4]   unknown / padding
          [5]      keypad sub-event type
          [6]      state or mode value (sub-event dependent)
          [7]      raw battery level (0–155 scale)
          [8]      signal strength (positive RSSI; negated by __init__)
          [9..]    PIN digits (only present for _KEYPAD_EVENT_PIN_CONFIRM,
                   count = data[0] - 6, ASCII digit bytes)
        """
        if len(data) < 9:
            _logger.warning("Short keypad HMS payload (%d bytes): %s", len(data), bytes_to_hex(data))
            return cls._parse_unknown(mac, event, sensor_type, timestamp, data)

        sub_event = data[5]
        state_byte = data[6]
        battery = data[7]
        signal_strength = data[8]

        if sub_event == _KEYPAD_EVENT_MODE:
            mode = _KEYPAD_MODE_STATES.get(state_byte, f"unknown:{state_byte:02X}")
            return cls(
                "keypad_mode",
                mac,
                timestamp,
                sensor_type="keypad",
                battery=battery,
                signal_strength=signal_strength,
                alarm_mode=mode,
            )

        if sub_event == _KEYPAD_EVENT_MOTION:
            motion = "active" if state_byte else "inactive"
            return cls(
                "keypad_motion",
                mac,
                timestamp,
                sensor_type="keypad",
                battery=battery,
                signal_strength=signal_strength,
                motion=motion,
            )

        if sub_event == _KEYPAD_EVENT_PIN_START:
            return cls(
                "keypad_pin_start",
                mac,
                timestamp,
                sensor_type="keypad",
                battery=battery,
                signal_strength=signal_strength,
            )

        if sub_event == _KEYPAD_EVENT_PIN_CONFIRM:
            # PIN digits follow at data[9..], count = data[0] - 6
            pin_len = max(0, data[0] - 6)
            pin_bytes = data[9 : 9 + pin_len]
            try:
                pin = pin_bytes.decode("ascii")
            except UnicodeDecodeError:
                _logger.warning("Non-ASCII PIN digits in keypad packet: %s", bytes_to_hex(pin_bytes))
                pin = ""
            return cls(
                "keypad_pin_confirm",
                mac,
                timestamp,
                sensor_type="keypad",
                battery=battery,
                signal_strength=signal_strength,
                pin=pin,
            )

        if sub_event == _KEYPAD_EVENT_ALARM:
            # Observed in AK5nowman's WyzeSense C# library as "Some sort of alarm event?"
            # Exact semantics unknown; bridge publishes it for automation use.
            return cls(
                "keypad_alarm",
                mac,
                timestamp,
                sensor_type="keypad",
                battery=battery,
                signal_strength=signal_strength,
                alarm_raw=state_byte,
            )

        _logger.warning("Unknown keypad sub-event 0x%02X: %s", sub_event, bytes_to_hex(data))
        return cls._parse_unknown(mac, event, sensor_type, timestamp, data)

    @classmethod
    def _parse_unknown(cls, mac: str, event: int, sensor_type: int, timestamp: float, data: bytes) -> "SensorEvent":
        return cls(f"unknown:{event:02X}", mac, timestamp, raw=bytes_to_hex(data))

    @classmethod
    def from_packet(cls, data: bytes) -> "SensorEvent":
        """Parse a v1 alarm/heartbeat/climate packet payload."""
        _EVENT_PARSERS = {
            _EVENT_HEARTBEAT: cls._parse_heartbeat,
            _EVENT_ALARM: cls._parse_alarm,
            _EVENT_CLIMATE: cls._parse_climate,
        }
        timestamp, event, mac_bytes, sensor_type = struct.unpack_from(">QB8sB", data)
        data = data[18:]
        timestamp /= 1000.0
        try:
            mac = mac_bytes.decode("ascii")
        except UnicodeDecodeError:
            _logger.warning("Non-ASCII MAC in v1 packet: %s", bytes_to_hex(mac_bytes))
            mac = mac_bytes.decode("latin-1")
        parser = _EVENT_PARSERS.get(event, cls._parse_unknown)
        return parser(mac, event, sensor_type, timestamp, data)

    @classmethod
    def from_packet_v2(cls, data: bytes) -> "SensorEvent":
        """Parse a v2 HMS packet payload (NOTIFY_SENSOR_ALARM2).

        Handles leak, climate, and keypad (HMS) packets.  The keypad uses a
        different internal structure from leak/climate — it has a sub-event
        type inside the payload rather than a top-level event byte — so it is
        dispatched separately based on sensor_type rather than event byte.
        """
        _EVENT_PARSERS = {
            _EVENT_LEAK: cls._parse_leak,
            _EVENT_CLIMATE: cls._parse_climate,
        }
        event, mac_bytes, sensor_type = struct.unpack_from(">B8sB", data)
        data = data[10:]
        timestamp = time.time()
        try:
            mac = mac_bytes.decode("ascii")
        except UnicodeDecodeError:
            _logger.warning("Non-ASCII MAC in v2 packet: %s", bytes_to_hex(mac_bytes))
            mac = mac_bytes.decode("latin-1")

        # Keypad packets use sensor_type 0x05 and carry sub-event structure
        # inside data rather than a meaningful top-level event byte.
        if sensor_type == SENSOR_TYPE_KEYPAD:
            return cls._parse_keypad(mac, event, sensor_type, timestamp, data)

        parser = _EVENT_PARSERS.get(event, cls._parse_unknown)
        return parser(mac, event, sensor_type, timestamp, data)


# ---------------------------------------------------------------------------
# Dongle – manages the USB HID connection and command/event loop
# ---------------------------------------------------------------------------


class Dongle:
    """Manages the USB HID connection to the WyzeSense bridge dongle."""

    _CMD_TIMEOUT = 2

    class _CmdContext:
        """Lightweight namespace for sharing state between a command and its handler closure."""

        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def __init__(self, device: str, event_handler):
        self._lock = threading.Lock()
        self._fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        self._exit_event = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="dongle-worker", daemon=True)
        self._event_handler = event_handler
        self._last_exception: Exception | None = None

        self._handlers: dict = {
            Packet.NOTIFY_SYNC_TIME: self._on_sync_time,
            Packet.NOTIFY_SENSOR_ALARM: self._on_sensor_alarm,
            Packet.NOTIFY_SENSOR_ALARM2: self._on_sensor_alarm2,
            Packet.NOTIFY_EVENT_LOG: self._on_event_log,
        }

        self._start()

    # ------------------------------------------------------------------
    # Public properties (set during _start)
    # ------------------------------------------------------------------

    mac: str
    version: str
    enr: bytes

    # ------------------------------------------------------------------
    # Async notification handlers
    # ------------------------------------------------------------------

    def _on_sensor_alarm(self, pkt: Packet) -> None:
        if len(pkt.payload) < 19:
            _logger.warning("Short alarm packet: %s", bytes_to_hex(pkt.payload))
            return
        event = SensorEvent.from_packet(pkt.payload)
        self._event_handler(self, event)

    def _on_sensor_alarm2(self, pkt: Packet) -> None:
        if len(pkt.payload) < 10:
            _logger.warning("Short alarm2 packet: %s", bytes_to_hex(pkt.payload))
            return
        event = SensorEvent.from_packet_v2(pkt.payload)
        self._event_handler(self, event)

    def _on_sync_time(self, pkt: Packet) -> None:
        self._send_packet(Packet.sync_time_ack())

    def _on_event_log(self, pkt: Packet) -> None:
        assert len(pkt.payload) >= 9
        ts, _msg_len = struct.unpack_from(">QB", pkt.payload)
        tm = datetime.datetime.fromtimestamp(ts / 1000.0)
        msg = pkt.payload[9:]
        _logger.debug("Dongle log: time=%s, data=%s", tm.isoformat(), bytes_to_hex(msg))
        if ts == 0:
            self._event_handler(self, "Dongle sent event log with timestamp=0 (clock not yet synchronized)")

    # ------------------------------------------------------------------
    # Internal transport
    # ------------------------------------------------------------------

    def _read_raw_hid(self) -> bytes:
        try:
            data = os.read(self._fd, 0x40)
        except OSError:
            return b""
        if not data:
            return b""
        data = bytes(data)
        length = data[0]
        assert length > 0
        if length > 0x3F:
            length = 0x3F
            _logger.warning("Truncating oversized HID packet")
        assert len(data) >= length + 1
        return data[1 : 1 + length]

    def _set_handler(self, cmd: int, handler) -> object:
        with self._lock:
            old = self._handlers.pop(cmd, None)
            if handler:
                self._handlers[cmd] = handler
        return old

    def _send_packet(self, pkt: Packet) -> None:
        _logger.debug("===> %s", pkt)
        pkt.send(self._fd)

    def _handle_packet(self, pkt: Packet) -> None:
        _logger.debug("<=== %s", pkt)
        with self._lock:
            handler = self._handlers.get(pkt.cmd, lambda p: None)
        if (pkt.cmd >> 8) == _TYPE_ASYNC and pkt.cmd != Packet.ASYNC_ACK:
            _logger.debug("Sending ACK for cmd %04X", pkt.cmd)
            self._send_packet(Packet.async_ack(pkt.cmd))
        handler(pkt)

    def _worker(self) -> None:
        try:
            buf = b""
            while not self._exit_event.is_set():
                buf += self._read_raw_hid()

                start = buf.find(b"\x55\xaa")
                if start == -1:
                    time.sleep(0.1)
                    continue

                buf = buf[start:]
                _logger.debug("Parsing: %s", bytes_to_hex(buf))
                try:
                    pkt = Packet.parse(buf)
                    if pkt is None:
                        _logger.error("Failed to parse packet – discarding")
                        buf = buf[2:]
                        time.sleep(0.1)
                        continue
                except EOFError:
                    time.sleep(0.1)
                    continue

                _logger.debug("Received: %s", bytes_to_hex(buf[: pkt.length]))
                buf = buf[pkt.length :]
                self._handle_packet(pkt)
        except Exception as exc:
            _logger.error("Dongle worker thread error", exc_info=True)
            self._last_exception = exc

    # ------------------------------------------------------------------
    # Command execution helpers
    # ------------------------------------------------------------------

    def _do_command(self, pkt: Packet, handler, timeout: int = _CMD_TIMEOUT) -> None:
        done = threading.Event()
        old = self._set_handler(pkt.cmd + 1, lambda p: handler(p, done))
        self._send_packet(pkt)
        if not done.wait(timeout):
            self._set_handler(pkt.cmd + 1, old)
            raise TimeoutError("Dongle command timed out")
        self._set_handler(pkt.cmd + 1, old)

    def _do_simple_command(self, pkt: Packet, timeout: int = _CMD_TIMEOUT) -> Packet:
        ctx = self._CmdContext(result=None)

        def _handler(response: Packet, done: threading.Event) -> None:
            ctx.result = response
            done.set()

        self._do_command(pkt, _handler, timeout)
        return ctx.result

    # ------------------------------------------------------------------
    # Initialisation sequence
    # ------------------------------------------------------------------

    def _do_inquiry(self) -> None:
        resp = self._do_simple_command(Packet.inquiry())
        assert len(resp.payload) == 1
        assert resp.payload[0] == 1, f"Inquiry failed, result={resp.payload[0]}"

    def _get_enr(self, r: list[int]) -> bytes:
        assert len(r) == 4
        r_bytes = struct.pack("<LLLL", *r)
        resp = self._do_simple_command(Packet.get_enr(r_bytes))
        assert len(resp.payload) == 16
        return resp.payload

    def _get_mac(self) -> str:
        resp = self._do_simple_command(Packet.get_mac())
        assert len(resp.payload) == 8
        try:
            return resp.payload.decode("ascii")
        except UnicodeDecodeError:
            _logger.warning("Non-ASCII dongle MAC: %s", bytes_to_hex(resp.payload))
            return bytes_to_hex(resp.payload)

    def _get_version(self) -> str:
        resp = self._do_simple_command(Packet.get_version())
        try:
            return resp.payload.decode("ascii")
        except UnicodeDecodeError:
            _logger.warning("Non-ASCII dongle version: %s", bytes_to_hex(resp.payload))
            return bytes_to_hex(resp.payload)

    def _get_sensor_r1(self, mac: str, r1: bytes) -> bytes:
        resp = self._do_simple_command(Packet.get_sensor_r1(mac, r1), timeout=10)
        return resp.payload

    def _finish_auth(self) -> None:
        resp = self._do_simple_command(Packet.finish_auth())
        assert len(resp.payload) == 0

    def _get_sensors(self) -> list[str]:
        resp = self._do_simple_command(Packet.get_sensor_count())
        assert len(resp.payload) == 1
        count = resp.payload[0]

        if count == 0:
            _logger.debug("No sensors paired")
            return []

        _logger.debug("%d sensor(s) paired, fetching list...", count)
        ctx = self._CmdContext(count=count, index=0, sensors=[])

        # Each response packet carries one MAC; the closure accumulates them
        # and sets done when the expected count is reached.
        def _handler(pkt: Packet, done: threading.Event) -> None:
            assert len(pkt.payload) == 8
            try:
                mac = pkt.payload.decode("ascii")
                _logger.debug("Sensor %d/%d: %s", ctx.index + 1, ctx.count, mac)
            except UnicodeDecodeError:
                mac = pkt.payload.decode("latin-1")
                _logger.warning(
                    "Sensor %d/%d has non-ASCII MAC: %s",
                    ctx.index + 1,
                    ctx.count,
                    bytes_to_hex(pkt.payload),
                )
            ctx.sensors.append(mac)
            ctx.index += 1
            if ctx.index == ctx.count:
                done.set()

        try:
            self._do_command(Packet.get_sensor_list(count), _handler, timeout=10)
        except TimeoutError:
            _logger.error(
                "Timeout fetching sensor list (received %d/%d): %s",
                ctx.index,
                ctx.count,
                ctx.sensors,
            )
            raise

        return ctx.sensors

    def _start(self) -> None:
        self._thread.start()
        try:
            self._do_inquiry()
            self.enr = self._get_enr([0x30303030] * 4)
            self.mac = self._get_mac()
            _logger.info("Dongle MAC: %s", self.mac)
            self.version = self._get_version()
            _logger.info("Dongle version: %s", self.version)
            self._finish_auth()
        except Exception:
            self.stop()
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list(self) -> list[str]:
        """Return the list of sensor MACs currently paired with the dongle."""
        return self._get_sensors()

    def scan(self, timeout: int = 60) -> tuple[str, str, str] | None:
        """Scan for a new sensor to pair.  Blocks up to *timeout* seconds.

        Returns ``(mac, type_name, version_str)`` on success, or ``None``.
        """
        _logger.debug("Starting sensor scan (timeout=%ds)...", timeout)
        self.check_error()

        ctx = self._CmdContext(done=threading.Event(), result=None)

        def _scan_handler(pkt: Packet) -> None:
            assert len(pkt.payload) == 11
            mac_bytes = pkt.payload[1:9]

            if mac_bytes in (b"\xff" * 8, b"\x00" * 8):
                _logger.warning("Scan received invalid MAC (%s)", bytes_to_hex(mac_bytes))
                ctx.result = None
                ctx.done.set()
                return

            try:
                mac = mac_bytes.decode("ascii")
            except UnicodeDecodeError:
                _logger.warning("Scan received non-ASCII MAC: %s", bytes_to_hex(mac_bytes))
                ctx.result = None
                ctx.done.set()
                return

            ctx.result = (mac, pkt.payload[9], pkt.payload[10])
            ctx.done.set()

        old_handler = self._set_handler(Packet.NOTIFY_SENSOR_SCAN, _scan_handler)
        try:
            self._do_simple_command(Packet.enable_scan())
            if ctx.done.wait(timeout):
                if ctx.result is not None:
                    s_mac, s_type, _s_ver = ctx.result
                    r1 = self._get_sensor_r1(s_mac, b"Ok5HPNQ4lf77u754")
                    _logger.debug("Sensor R1: %s", bytes_to_hex(r1))
            else:
                _logger.debug("Sensor scan timed out — no sensor found in pairing mode")
            self._do_simple_command(Packet.disable_scan())
        finally:
            self._set_handler(Packet.NOTIFY_SENSOR_SCAN, old_handler)

        if ctx.result:
            s_mac, s_type, s_ver = ctx.result
            self._do_simple_command(Packet.verify_sensor(s_mac), 10)
            type_name = SENSOR_TYPE_NAMES.get(s_type, f"unknown:{s_type:02X}")
            return (s_mac, type_name, str(s_ver))

        return None

    def delete(self, mac: str) -> None:
        """Unpair a sensor by MAC address."""
        if "," in mac:
            # Comma-separated hex bytes (non-ASCII MAC displayed by bridge_tool)
            mac_bytes = bytes(int(x, 16) for x in mac.split(","))
            resp = self._do_simple_command(Packet.del_sensor(mac_bytes))
        else:
            resp = self._do_simple_command(Packet.del_sensor(mac))

        _logger.debug("del_sensor response: %s", bytes_to_hex(resp.payload))
        assert len(resp.payload) == 9
        try:
            ack_mac = resp.payload[:8].decode("ascii")
        except UnicodeDecodeError:
            _logger.warning("Non-ASCII MAC in delete response: %s", bytes_to_hex(resp.payload[:8]))
            ack_mac = resp.payload[:8].decode("latin-1")

        ack_code = resp.payload[8]
        assert ack_code == 0xFF, f"del_sensor: unexpected ACK code 0x{ack_code:02X}"

        ack_bytes = ack_mac.encode("latin-1") if len(ack_mac) == 8 else ack_mac.encode("ascii")
        try:
            req_bytes = mac.encode("ascii") if len(mac) == 8 else mac.encode("latin-1")
        except UnicodeEncodeError:
            req_bytes = mac.encode("latin-1")

        assert ack_bytes == req_bytes, (
            f"del_sensor MAC mismatch: requested {bytes_to_hex(req_bytes)}, got {bytes_to_hex(ack_bytes)}"
        )
        _logger.debug("Deleted sensor: %s", bytes_to_hex(req_bytes) if not mac.isascii() else mac)

    def delete_all(self) -> None:
        """Unpair all sensors from the dongle."""
        resp = self._do_simple_command(Packet.del_all_sensors())
        assert len(resp.payload) == 1
        assert resp.payload[0] == 0xFF, f"del_all_sensors: unexpected ACK code 0x{resp.payload[0]:02X}"

    def play_chime(self, mac: str, ring_id: int, repeat_count: int, volume: int) -> None:
        """Play a chime tone on a paired chime sensor."""
        self._do_simple_command(Packet.play_chime(mac, ring_id, repeat_count, volume))

    def send_keypad_status(self, mac: str, state_byte: int) -> None:
        """Push the current alarm state to the keypad display/LEDs.

        This sends CMD_SEND_KEYPAD_EVENT (0x53/0x53) to the dongle, which
        relays it to the paired keypad over RF.  The keypad updates its
        display and indicator LEDs to reflect the new state.

        State byte values (confirmed by PR + AK5nowman/WyzeSense):
          0x01 = disarmed
          0x02 = armed_home
          0x03 = armed_away
          0x04 = triggered/alarm

        Note: This command's full effect on the keypad (sounds, LED colours)
        has not been confirmed with physical hardware.  Feedback welcome —
        see docs/contributing_protocol.md.
        """
        self._do_simple_command(Packet.send_keypad_status(mac, state_byte))

    def send_raw(self, data: bytes) -> None:
        """Send a raw packet (for diagnostics / testing)."""
        _logger.debug("Sending raw: %s", bytes_to_hex(data))
        pkt = Packet.parse(data)
        self._do_simple_command(pkt)

    def check_error(self) -> None:
        """Re-raise any exception that occurred in the worker thread."""
        if self._last_exception:
            raise self._last_exception

    def stop(self, timeout: int = _CMD_TIMEOUT) -> None:
        """Stop the worker thread and close the HID device."""
        self._exit_event.set()
        self._thread.join(timeout)
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------


def open_dongle(device: str, event_handler, logger: logging.Logger | None = None) -> Dongle:
    """Open the WyzeSense dongle at *device* and return a connected :class:`Dongle`.

    Args:
        device:        Path to the HID device, e.g. ``'/dev/hidraw0'``.
        event_handler: Callable ``(dongle, event)`` invoked for each sensor event.
        logger:        Optional logger; falls back to the module logger.
    """
    global _logger
    if logger is not None:
        _logger = logger
    else:
        _logger = logging.getLogger("ws2m.dongle")
    return Dongle(device, event_handler)
