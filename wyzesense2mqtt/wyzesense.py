import binascii

from builtins import bytes
from builtins import str

import datetime
import logging
import os
import struct
import threading
import time

log = logging.getLogger(__name__)
TYPE_SYNC = 0x43
TYPE_ASYNC = 0x53


def bytes_to_hex(s):
    if s:
        return binascii.hexlify(s)
    else:
        return "<None>"


def checksum_from_bytes(s):
    return sum(bytes(s)) & 0xFFFF


def MAKE_CMD(type, cmd):
    return (type << 8) | cmd


class Packet(object):
    _CMD_TIMEOUT = 5

    # Sync packets:
    # Commands initiated from host side
    CMD_GET_ENR = MAKE_CMD(TYPE_SYNC, 0x02)
    CMD_GET_MAC = MAKE_CMD(TYPE_SYNC, 0x04)
    CMD_GET_KEY = MAKE_CMD(TYPE_SYNC, 0x06)
    CMD_INQUIRY = MAKE_CMD(TYPE_SYNC, 0x27)
    CMD_UPDATE_CC1310 = MAKE_CMD(TYPE_SYNC, 0x12)
    CMD_SET_CH554_UPGRADE = MAKE_CMD(TYPE_SYNC, 0x0E)

    # Async packets:
    ASYNC_ACK = MAKE_CMD(TYPE_ASYNC, 0xFF)

    # Commands initiated from dongle side
    CMD_FINISH_AUTH = MAKE_CMD(TYPE_ASYNC, 0x14)
    CMD_GET_DONGLE_VERSION = MAKE_CMD(TYPE_ASYNC, 0x16)
    CMD_START_STOP_SCAN = MAKE_CMD(TYPE_ASYNC, 0x1C)
    CMD_GET_SENSOR_R1 = MAKE_CMD(TYPE_ASYNC, 0x21)
    CMD_VERIFY_SENSOR = MAKE_CMD(TYPE_ASYNC, 0x23)
    CMD_DEL_SENSOR = MAKE_CMD(TYPE_ASYNC, 0x25)
    CMD_GET_SENSOR_COUNT = MAKE_CMD(TYPE_ASYNC, 0x2E)
    CMD_GET_SENSOR_LIST = MAKE_CMD(TYPE_ASYNC, 0x30)

    # Notifications initiated from dongle side
    NOTIFY_SENSOR_EVENT = MAKE_CMD(TYPE_ASYNC, 0x19)
    NOTIFY_SENSOR_SCAN = MAKE_CMD(TYPE_ASYNC, 0x20)
    NOITFY_SYNC_TIME = MAKE_CMD(TYPE_ASYNC, 0x32)
    NOTIFY_EVENT_LOG = MAKE_CMD(TYPE_ASYNC, 0x35)
    NOTIFY_HMS_EVENT = MAKE_CMD(TYPE_ASYNC, 0x55)

    def __init__(self, cmd, payload=bytes()):
        self._cmd = cmd
        if self._cmd == self.ASYNC_ACK:
            assert isinstance(payload, int)
        else:
            assert isinstance(payload, bytes)
        self._payload = payload

    def __str__(self):
        if self._cmd == self.ASYNC_ACK:
            return "Packet: Cmd=%04X, Payload=ACK(%04X)" % (self._cmd, self._payload)
        else:
            return "Packet: Cmd=%04X, Payload=%s" % (
                self._cmd,
                bytes_to_hex(self._payload),
            )

    @property
    def Length(self):
        if self._cmd == self.ASYNC_ACK:
            return 7
        else:
            return len(self._payload) + 7

    @property
    def Cmd(self):
        return self._cmd

    @property
    def Payload(self):
        return self._payload

    def Send(self, fd):
        pkt = bytes()

        pkt += struct.pack(">HB", 0xAA55, self._cmd >> 8)
        if self._cmd == self.ASYNC_ACK:
            pkt += struct.pack("BB", (self._payload & 0xFF), self._cmd & 0xFF)
        else:
            pkt += struct.pack("BB", len(self._payload) + 3, self._cmd & 0xFF)
            if self._payload:
                pkt += self._payload

        checksum = checksum_from_bytes(pkt)
        pkt += struct.pack(">H", checksum)
        log.debug("Sending: %s", bytes_to_hex(pkt))
        ss = os.write(fd, pkt)
        assert ss == len(pkt)

    @classmethod
    def Parse(cls, s):
        assert isinstance(s, bytes)

        if len(s) < 5:
            log.error("Invalid packet: %s", bytes_to_hex(s))
            log.error("Invalid packet length: %d", len(s))
            return None

        magic, cmd_type, b2, cmd_id = struct.unpack_from(">HBBB", s)
        if magic != 0x55AA and magic != 0xAA55:
            log.error("Invalid packet: %s", bytes_to_hex(s))
            log.error("Invalid packet magic: %4X", magic)
            return None

        cmd = MAKE_CMD(cmd_type, cmd_id)
        if cmd == cls.ASYNC_ACK:
            assert len(s) >= 7
            s = s[:7]
            payload = MAKE_CMD(cmd_type, b2)
        elif len(s) >= b2 + 4:
            s = s[: b2 + 4]
            payload = s[5:-2]
        else:
            log.error("Invalid packet: %s", bytes_to_hex(s))
            return None

        cs_remote = (s[-2] << 8) | s[-1]
        cs_local = checksum_from_bytes(s[:-2])
        if cs_remote != cs_local:
            log.error("Invalid packet: %s", bytes_to_hex(s))
            log.error(
                "Mismatched checksum, remote=%04X, local=%04X", cs_remote, cs_local
            )
            return None

        return cls(cmd, payload)

    @classmethod
    def GetVersion(cls):
        return cls(cls.CMD_GET_DONGLE_VERSION)

    @classmethod
    def Inquiry(cls):
        return cls(cls.CMD_INQUIRY)

    @classmethod
    def GetEnr(cls, r):
        assert isinstance(r, bytes)
        assert len(r) == 16
        return cls(cls.CMD_GET_ENR, r)

    @classmethod
    def GetMAC(cls):
        return cls(cls.CMD_GET_MAC)

    @classmethod
    def GetKey(cls):
        return cls(cls.CMD_GET_KEY)

    @classmethod
    def EnableScan(cls):
        return cls(cls.CMD_START_STOP_SCAN, b"\x01")

    @classmethod
    def DisableScan(cls):
        return cls(cls.CMD_START_STOP_SCAN, b"\x00")

    @classmethod
    def GetSensorCount(cls):
        return cls(cls.CMD_GET_SENSOR_COUNT)

    @classmethod
    def GetSensorList(cls, count):
        assert count <= 0xFF
        return cls(cls.CMD_GET_SENSOR_LIST, struct.pack("B", count))

    @classmethod
    def FinishAuth(cls):
        return cls(cls.CMD_FINISH_AUTH, b"\xFF")

    @classmethod
    def DelSensor(cls, mac):
        assert isinstance(mac, str)
        assert len(mac) == 8
        return cls(cls.CMD_DEL_SENSOR, mac.encode('ascii'))

    @classmethod
    def GetSensorR1(cls, mac, r):
        assert isinstance(r, bytes)
        assert len(r) == 16
        assert isinstance(mac, str)
        assert len(mac) == 8
        return cls(cls.CMD_GET_SENSOR_R1, mac.encode('ascii') + r)

    @classmethod
    def VerifySensor(cls, mac):
        assert isinstance(mac, str)
        assert len(mac) == 8
        return cls(cls.CMD_VERIFY_SENSOR, mac.encode('ascii') + b"\xFF\x04")

    @classmethod
    def UpdateCC1310(cls):
        return cls(cls.CMD_UPDATE_CC1310)

    @classmethod
    def Ch554Upgrade(cls):
        return cls(cls.CMD_SET_CH554_UPGRADE)

    @classmethod
    def SyncTimeAck(cls):
        return cls(cls.NOITFY_SYNC_TIME + 1, struct.pack(">Q", int(time.time() * 1000)))

    @classmethod
    def AsyncAck(cls, cmd):
        assert (cmd >> 0x8) == TYPE_ASYNC
        return cls(cls.ASYNC_ACK, cmd)


class SensorEvent(object):
    def __init__(self, mac, timestamp, event_type, event_data, pkt=None):
        self.MAC = mac
        self.Timestamp = timestamp
        self.Type = event_type
        self.Data = event_data
        self.Payload = pkt

    def __str__(self):
        formatted_time = self.Timestamp.strftime("%Y-%m-%d %H:%M:%S")
        s = f"[{formatted_time}][{self.MAC}]\n"
        if self.Payload is not None:
            s += "Payload: "
            for idx, i in enumerate(self.Payload):
                s += f"{idx}: {i}, "
            s += "\n"
        if self.Type in ['alarm', 'status', 'keypad']:
            s += f"{self.Type.title()}Event: event_type={self.Data[0]}, state={self.Data[1]}, battery={self.Data[2]}, signal={self.Data[3]}"
        else:
            s += f"RawEvent: type={self.Type}, data={bytes_to_hex(self.Data)}"
        return s


class Dongle(object):
    _CMD_TIMEOUT = 2

    def __init__(self, device, event_handler):
        self.__lock = threading.Lock()
        self.__fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        self.__exit_event = threading.Event()
        self.__thread = threading.Thread(target=self._Worker)
        self.__on_event = event_handler

        self.__handlers = {
            Packet.NOTIFY_SENSOR_EVENT: self._OnSensorEvent,
            Packet.NOITFY_SYNC_TIME: self._OnSyncTime,
            Packet.NOTIFY_EVENT_LOG: self._OnEventLog,
            Packet.NOTIFY_HMS_EVENT: self._OnHMSEvent,
        }

        self._Start()

    class CmdContext(object):
        def __init__(self, **kwargs):
            for key in kwargs:
                setattr(self, key, kwargs[key])

    def _OnSensorEvent(self, pkt):
        if len(pkt.Payload) < 18:
            log.info(f"Unknown event packet: {bytes_to_hex(pkt.Payload)}")
            return

        timestamp, event_type, sensor_mac = struct.unpack_from(">QB8s", pkt.Payload)
        timestamp = datetime.datetime.fromtimestamp(timestamp / 1000.0)
        sensor_mac = sensor_mac.decode('ascii')
        event_data = pkt.Payload[17:]

        event_types = {0xA1: "status", 0xA2: "alarm", 0xE8: "leak"}

        # {sensor_id: "sensor type", "states": ["off state", "on state"]}
        contact_ids = {0x01: "switch", 0x0E: "switchv2", "states": ["close", "open"]}
        motion_ids = {
            0x02: "motion",
            0x0F: "motionv2",
            "states": ["inactive", "active"],
        }
        leak_ids = {0x03: "leak", "states": ["dry", "wet"]}
        keypad_ids = {0x05: "keypad", "states": ["pair"]}

        if event_type in [0xA1, 0xA2]:
            sensor = {}
            for i in [contact_ids, motion_ids, leak_ids, keypad_ids]:
                if event_data[0] in i:
                    sensor = i

            sensor_type = (
                sensor[event_data[0]]
                if sensor
                else "unknown ({})".format(event_data[0])
            )
            sensor_state = (
                sensor["states"][event_data[5]]
                if sensor
                else "unknown ({})".format(event_data[5])
            )

            battery = int(
                event_data[2] / 155 * 100 if event_type in keypad_ids else event_data[2]
            )

            e = SensorEvent(
                sensor_mac,
                timestamp,
                event_types[event_type],
                (sensor_type, sensor_state, battery, event_data[-1]),
            )
        elif event_type == 0xE8:
            if event_data[0] == 0x03:
                # alarm_data[7] might be humidity in some form, but as an integer
                # is reporting way to high to actually be humidity.
                sensor_type = "leak:temperature"
                sensor_state = "%d.%d" % (event_data[5], event_data[6])
                e = SensorEvent(
                    sensor_mac,
                    timestamp,
                    "state",
                    (sensor_type, sensor_state, event_data[2], event_data[8]),
                )
        else:
            e = SensorEvent(sensor_mac, timestamp, "raw_%02X" % event_type, event_data)

        self.__on_event(self, e)

    def _OnSyncTime(self, pkt):
        self._SendPacket(Packet.SyncTimeAck())

    def _OnEventLog(self, pkt):
        assert len(pkt.Payload) >= 9
        ts, msg_len = struct.unpack_from(">QB", pkt.Payload)
        # assert msg_len + 8 == len(pkt.Payload)
        tm = datetime.datetime.fromtimestamp(ts / 1000.0)
        msg = pkt.Payload[9:]
        log.info(f"LOG: time={tm.isoformat()}, data={bytes_to_hex(msg)}")

    def _OnHMSEvent(self, pkt):
        _, sensor_mac = struct.unpack_from(">B8s", pkt.Payload)
        sensor_mac = sensor_mac.decode('ascii')
        timestamp = datetime.datetime.utcnow()

        event_data = pkt.Payload[10:]
        event_type = event_data[4]
        battery = int(event_data[2] / 155 * 100)

        mode_ids = {
            0x02: "mode",
            "states": ["unknown", "disarmed", "armed_home", "armed_away", "triggered"],
        }
        motion_ids = {
            0x0A: "motion",
            "states": ["inactive", "active"],
        }

        pin_ids = {0x06: "pinStart", 0x08: "pinConfirm"}

        sensor = {}
        if event_type in [0x02, 0x0A]:
            for i in [mode_ids, motion_ids]:
                if event_type in i:
                    sensor = i

            sensor_type = (
                sensor[event_type] if sensor else "unknown ({})".format(event_type)
            )
            sensor_state = (
                sensor["states"][event_data[5]]
                if sensor
                else "unknown ({})".format(event_data[5])
            )

            e = SensorEvent(
                sensor_mac,
                timestamp,
                "keypad",
                (sensor_type, sensor_state, battery, event_data[-1]),
            )
        elif event_type in [0x06, 0x08]:
            for i in [pin_ids]:
                if event_type in i:
                    sensor = i

            if sensor:
                pin = (
                    False
                    if event_type == 0x06
                    else "".join(
                        [str(d) for d in event_data[5 : (5 + event_data[0] - 6)]]
                    )
                )

                sensor_type = sensor[event_type]
                sensor_state = pin
            else:
                sensor_type = "unknown ({})".format(event_type)
                sensor_state = "unknown ({})".format(event_type)

            e = SensorEvent(
                sensor_mac,
                timestamp,
                "keypad",
                (sensor_type, sensor_state, battery, event_data[-1]),
                pkt.Payload,
            )
        else:
            e = SensorEvent(sensor_mac, timestamp, "raw_%02X" % event_type, event_data)

        self.__on_event(self, e)

    def _ReadRawHID(self):
        try:
            s = os.read(self.__fd, 0x40)
        except OSError:
            return b""

        if not s:
            log.info("Nothing read")
            return b""

        s = bytes(s)
        length = s[0]
        assert length > 0
        if length > 0x3F:
            length = 0x3F

        # log.debug("Raw HID packet: %s", bytes_to_hex(s))
        assert len(s) >= length + 1
        return s[1 : 1 + length]

    def _SetHandler(self, cmd, handler):
        with self.__lock:
            oldHandler = self.__handlers.pop(cmd, None)
            if handler:
                self.__handlers[cmd] = handler
        return oldHandler

    def _SendPacket(self, pkt):
        log.debug("===> Sending: %s", str(pkt))
        pkt.Send(self.__fd)

    def _DefaultHandler(self, pkt):
        pass

    def _HandlePacket(self, pkt):
        log.debug("<=== Received: %s", str(pkt))
        with self.__lock:
            handler = self.__handlers.get(pkt.Cmd, self._DefaultHandler)

        if (pkt.Cmd >> 8) == TYPE_ASYNC and pkt.Cmd != Packet.ASYNC_ACK:
            # log.info("Sending ACK packet for cmd %04X", pkt.Cmd)
            self._SendPacket(Packet.AsyncAck(pkt.Cmd))
        handler(pkt)

    def _Worker(self):
        s = b""
        while True:
            if self.__exit_event.isSet():
                break

            s += self._ReadRawHID()
            # if s:
            #     log.info("Incoming buffer: %s", bytes_to_hex(s))
            start = s.find(b"\x55\xAA")
            if start == -1:
                time.sleep(0.1)
                continue

            s = s[start:]
            log.debug("Trying to parse: %s", bytes_to_hex(s))
            pkt = Packet.Parse(s)
            if not pkt:
                s = s[2:]
                continue

            log.debug("Received: %s", bytes_to_hex(s[: pkt.Length]))
            s = s[pkt.Length :]
            self._HandlePacket(pkt)

    def _DoCommand(self, pkt, handler, timeout=_CMD_TIMEOUT):
        e = threading.Event()
        oldHandler = self._SetHandler(pkt.Cmd + 1, lambda pkt: handler(pkt, e))
        self._SendPacket(pkt)
        result = e.wait(timeout)
        self._SetHandler(pkt.Cmd + 1, oldHandler)

        if not result:
            raise TimeoutError("_DoCommand")

    def _DoSimpleCommand(self, pkt, timeout=_CMD_TIMEOUT):
        ctx = self.CmdContext(result=None)

        def cmd_handler(pkt, e):
            ctx.result = pkt
            e.set()

        self._DoCommand(pkt, cmd_handler, timeout)
        return ctx.result

    def _Inquiry(self):
        log.debug("Start Inquiry...")
        resp = self._DoSimpleCommand(Packet.Inquiry())

        assert len(resp.Payload) == 1
        result = resp.Payload[0]
        log.debug("Inquiry returns %d", result)

        assert result == 1, "Inquiry failed, result=%d" % result

    def _GetEnr(self, r):
        log.debug("Start GetEnr...")
        assert len(r) == 4
        assert all(isinstance(x, int) for x in r)
        r_string = bytes(struct.pack("<LLLL", *r))

        resp = self._DoSimpleCommand(Packet.GetEnr(r_string))
        assert len(resp.Payload) == 16
        log.debug("GetEnr returns %s", bytes_to_hex(resp.Payload))
        return resp.Payload

    def _GetMac(self):
        log.debug("Start GetMAC...")
        resp = self._DoSimpleCommand(Packet.GetMAC())
        assert len(resp.Payload) == 8
        mac = resp.Payload.decode('ascii')
        log.debug("GetMAC returns %s", mac)
        return mac

    def _GetKey(self):
        log.debug("Start GetKey...")
        resp = self._DoSimpleCommand(Packet.GetKey())
        assert len(resp.Payload) == 16
        log.debug("GetKey returns %s", resp.Payload)
        return resp.Payload

    def _GetVersion(self):
        log.debug("Start GetVersion...")
        resp = self._DoSimpleCommand(Packet.GetVersion())
        version = resp.Payload.decode('ascii')
        log.debug("GetVersion returns %s", version)
        return version

    def _GetSensorR1(self, mac, r1):
        log.debug("Start GetSensorR1...")
        resp = self._DoSimpleCommand(Packet.GetSensorR1(mac, r1))
        return resp.Payload

    def _EnableScan(self):
        log.debug("Start EnableScan...")
        resp = self._DoSimpleCommand(Packet.EnableScan())
        assert len(resp.Payload) == 1
        result = resp.Payload[0]
        assert result == 0x01, "EnableScan failed, result=%d"

    def _DisableScan(self):
        log.debug("Start DisableScan...")
        resp = self._DoSimpleCommand(Packet.DisableScan())
        assert len(resp.Payload) == 1
        result = resp.Payload[0]
        assert result == 0x01, "DisableScan failed, result=%d"

    def _GetSensors(self):
        log.debug("Start GetSensors...")

        resp = self._DoSimpleCommand(Packet.GetSensorCount())
        assert len(resp.Payload) == 1
        count = resp.Payload[0]

        ctx = self.CmdContext(count=count, index=0, sensors=[])
        if count > 0:
            log.debug("%d sensors reported, waiting for each one to report...", count)

            def cmd_handler(pkt, e):
                assert len(pkt.Payload) == 8
                mac = pkt.Payload.decode('ascii')
                log.debug("Sensor %d/%d, MAC:%s", ctx.index + 1, ctx.count, mac)

                ctx.sensors.append(mac)
                ctx.index += 1
                if ctx.index == ctx.count:
                    e.set()

            self._DoCommand(
                Packet.GetSensorList(count),
                cmd_handler,
                timeout=self._CMD_TIMEOUT * count,
            )
        else:
            log.debug("No sensors bond yet...")
        return ctx.sensors

    def _FinishAuth(self):
        resp = self._DoSimpleCommand(Packet.FinishAuth())
        assert len(resp.Payload) == 0

    def _Start(self):
        self.__thread.start()

        try:
            self._Inquiry()

            self.ENR = self._GetEnr([0x30303030] * 4)
            self.MAC = self._GetMac()
            log.debug("Dongle MAC is [%s]", self.MAC)

            self.Version = self._GetVersion()
            log.debug("Dongle version: %s", self.Version)

            self._FinishAuth()
        except:
            self.Stop()
            raise

    def List(self):
        sensors = self._GetSensors()
        for x in sensors:
            log.debug("Sensor found: %s", x)

        return sensors

    def Stop(self, timeout=_CMD_TIMEOUT):
        self.__exit_event.set()
        os.close(self.__fd)
        self.__fd = None
        self.__thread.join(timeout)

    def Scan(self, timeout=60):
        log.debug("Start Scan...")

        ctx = self.CmdContext(evt=threading.Event(), result=None)

        def scan_handler(pkt):
            assert len(pkt.Payload) == 11
            ctx.result = (
                pkt.Payload[1:9].decode('ascii'),
                pkt.Payload[9],
                pkt.Payload[10],
            )
            ctx.evt.set()

        old_handler = self._SetHandler(Packet.NOTIFY_SENSOR_SCAN, scan_handler)
        try:
            self._DoSimpleCommand(Packet.EnableScan())

            if ctx.evt.wait(timeout):
                s_mac, s_type, s_ver = ctx.result
                log.debug(
                    f"Sensor found: mac=[{s_mac}], type={s_type}, version={s_ver}"
                )
                r1 = self._GetSensorR1(s_mac, b'Ok5HPNQ4lf77u754')
                log.debug(f"Sensor R1: {bytes_to_hex(r1)}")
            else:
                log.debug("Sensor discovery timeout...")

            self._DoSimpleCommand(Packet.DisableScan())
        finally:
            self._SetHandler(Packet.NOTIFY_SENSOR_SCAN, old_handler)
        if ctx.result:
            s_mac, s_type, s_ver = ctx.result
            self._DoSimpleCommand(Packet.VerifySensor(s_mac))
        return ctx.result

    def Delete(self, mac):
        resp = self._DoSimpleCommand(Packet.DelSensor(str(mac)))
        log.debug(f"CmdDelSensor returns {bytes_to_hex(resp.Payload)}")
        assert len(resp.Payload) == 9
        ack_mac = resp.Payload[:8].decode('ascii')
        ack_code = resp.Payload[8]
        assert ack_code == 0xFF, "CmdDelSensor: Unexpected ACK code: 0x%02X" % ack_code
        assert (
            ack_mac == mac
        ), f"CmdDelSensor: MAC mismatch, requested:{mac}, returned:{ack_mac}"
        log.debug(f"CmdDelSensor: {mac} deleted")


def Open(device, event_handler):
    return Dongle(device, event_handler)
