from builtins import bytes
from builtins import str

import os
import time
import struct
import threading
import datetime
import binascii

import logging


def bytes_to_hex(s):
    if s:
        return binascii.hexlify(s)
    else:
        return "<None>"


def checksum_from_bytes(s):
    return sum(bytes(s)) & 0xFFFF


TYPE_SYNC = 0x43
TYPE_ASYNC = 0x53

# {sensor_id: "sensor type", "states": ["off state", "on state"]}
CONTACT_IDS = {0x01: "switch", 0x0E: "switchv2", "states": ["close", "open"]}
MOTION_IDS = {0x02: "motion", 0x0F: "motionv2", "states": ["inactive", "active"]}
LEAK_IDS = {0x03: "leak", "states": ["dry", "wet"]}
KEYPAD_IDS = {0x05: "keypad", "states": ["pair"]}


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
    NOTIFY_SYNC_TIME = MAKE_CMD(TYPE_ASYNC, 0x32)
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
            return "Packet: Cmd=%04X, Payload=%s" % (self._cmd, bytes_to_hex(self._payload))

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
        LOGGER.debug("Sending: %s", bytes_to_hex(pkt))
        ss = os.write(fd, pkt)
        assert ss == len(pkt)

    @classmethod
    def Parse(cls, s):
        assert isinstance(s, bytes)

        if len(s) < 5:
            LOGGER.error("Invalid packet: %s", bytes_to_hex(s))
            LOGGER.error("Invalid packet length: %d", len(s))
            # This error can be corrected by waiting for additional data, throw an exception we can catch to handle differently
            raise EOFError

        magic, cmd_type, b2, cmd_id = struct.unpack_from(">HBBB", s)
        if magic != 0x55AA and magic != 0xAA55:
            LOGGER.error("Invalid packet: %s", bytes_to_hex(s))
            LOGGER.error("Invalid packet magic: %4X", magic)
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
            LOGGER.error("Invalid packet: %s", bytes_to_hex(s))
            LOGGER.error("Short packet: expected %d, got %d", (b2 + 4), len(s))
            # This error can be corrected by waiting for additional data, throw an exception we can catch to handle differently
            raise EOFError

        cs_remote = (s[-2] << 8) | s[-1]
        cs_local = checksum_from_bytes(s[:-2])
        if cs_remote != cs_local:
            LOGGER.error("Invalid packet: %s", bytes_to_hex(s))
            LOGGER.error("Mismatched checksum, remote=%04X, local=%04X", cs_remote, cs_local)
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
        return cls(cls.NOTIFY_SYNC_TIME + 1, struct.pack(">Q", int(time.time() * 1000)))

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
        if self.Type == 'alarm':
            return f"AlarmEvent [{self.MAC}]: time={self.Timestamp.strftime('%Y-%m-%d %H:%M:%S')}, sensor_type={self.Data[0]}, state={self.Data[1]}, battery={self.Data[2]}, signal={self.Data[3]}"
        elif self.Type == 'status':
            return f"StatusEvent [{self.MAC}]: time={self.Timestamp.strftime('%Y-%m-%d %H:%M:%S')}, sensor_type={self.Data[0]}, state={self.Data[1]}, battery={self.Data[2]}, signal={self.Data[3]}"
        elif self.Type == 'keypad':
            return f"KeypadEvent [{self.MAC}]: time={self.Timestamp.strftime('%Y-%m-%d %H:%M:%S')}, sensor_type={self.Data[0]}, state={self.Data[1]}, battery={self.Data[2]}, signal={self.Data[3]}"
        else:
            return f"RawEvent [{self.MAC}]: time={self.Timestamp.strftime('%Y-%m-%d %H:%M:%S')}, event_type={self.Type}, data={bytes_to_hex(self.Data)}"


class Dongle(object):
    _CMD_TIMEOUT = 2

    class CmdContext(object):
        def __init__(self, **kwargs):
            for key in kwargs:
                setattr(self, key, kwargs[key])

    def _OnSensorAlarm(self, pkt):
        global CONTACT_IDS, MOTION_IDS, LEAK_IDS, KEYPAD_IDS

        if len(pkt.Payload) < 18:
            LOGGER.info("Unknown alarm packet: %s", bytes_to_hex(pkt.Payload))
            return

        timestamp, event, mac = struct.unpack_from(">QB8s", pkt.Payload)
        data = pkt.Payload[17:]
        timestamp = datetime.datetime.fromtimestamp(timestamp / 1000.0)
        mac = mac.decode('ascii')

        if event == 0xA2 or event == 0xA1:
            type, b1, battery, b2, state1, state2, counter, signal = struct.unpack_from(">BBBBBBHB", data)
            sensor = {}
            if type in CONTACT_IDS:
                sensor = CONTACT_IDS
            elif type in MOTION_IDS:
                sensor = MOTION_IDS
            elif type in LEAK_IDS:
                sensor = LEAK_IDS
            elif type in KEYPAD_IDS:
                sensor = KEYPAD_IDS
                battery = battery / 155 * 100

            if sensor:
                sensor_type = sensor[type]
                sensor_state = sensor["states"][state2]
            else:
                sensor_type = f"unknown({type:02X})"
                sensor_state = f"unknown({state2:02X})"
            e = SensorEvent(mac, timestamp, ("alarm" if event == 0xA2 else "status"), (sensor_type, sensor_state, battery, signal))
        elif event == 0xE8:
            type, b1, battery, b2, state1, state2, counter, signal = struct.unpack_from(">BBBBBBHB", data)
            if type == 0x03:
                sensor_type = "leak:temperature"
                sensor_state = "%d.%d" % (state1, state2)
            else:
                sensor_type = f"unknown({type:02X})"
                sensor_state = f"unknown({state2:02X})"
            e = SensorEvent(mac, timestamp, "status", (sensor_type, sensor_state, battery, signal))
        else:
            e = SensorEvent(mac, timestamp, f"{event:02X}", data)

        self.__on_event(self, e)

    def _OnSyncTime(self, pkt):
        self._SendPacket(Packet.SyncTimeAck())

    def _OnEventLog(self, pkt):
        assert len(pkt.Payload) >= 9
        ts, msg_len = struct.unpack_from(">QB", pkt.Payload)
        tm = datetime.datetime.fromtimestamp(ts / 1000.0)
        msg = pkt.Payload[9:]
        LOGGER.info("LOG: time=%s, data=%s", tm.isoformat(), bytes_to_hex(msg))
        if ts == 0:
            self.__on_event(self, "ERROR")
        # Check if we have a message after, length includes the msglen byte
#        if ((len(msg) + 1) >= msg_len and msg_len >= 13):
#            event, mac, type, state, counter = struct.unpack(">B8sBBH", msg)
# TODO: What can we do with this? At the very least, we can update the last seen time for the sensor
# and it appears that the log message happens before every alarm message, so doesn't really gain much of anything

    def __init__(self, device, event_handler):
        self.__lock = threading.Lock()
        self.__fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        self.__sensors = {}
        self.__exit_event = threading.Event()
        self.__thread = threading.Thread(target=self._Worker)
        self.__on_event = event_handler
        self.__last_exception = None

        self.__handlers = {
            Packet.NOTIFY_SYNC_TIME: self._OnSyncTime,
            Packet.NOTIFY_SENSOR_ALARM: self._OnSensorAlarm,
            Packet.NOTIFY_EVENT_LOG: self._OnEventLog,
            Packet.NOTIFY_HMS_EVENT: self._OnHMSEvent,

        self._Start()

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
            LOGGER.info("Nothing read")
            return b""

        s = bytes(s)
        length = s[0]
        assert length > 0
        if length > 0x3F:
            length = 0x3F
            LOGGER.warn("Shortening a packet")

        # LOGGER.debug("Raw HID packet: %s", bytes_to_hex(s))
        assert len(s) >= length + 1
        return s[1: 1 + length]

    def _SetHandler(self, cmd, handler):
        with self.__lock:
            oldHandler = self.__handlers.pop(cmd, None)
            if handler:
                self.__handlers[cmd] = handler
        return oldHandler

    def _SendPacket(self, pkt):
        LOGGER.debug("===> Sending: %s", str(pkt))
        pkt.Send(self.__fd)

    def _DefaultHandler(self, pkt):
        pass

    def _HandlePacket(self, pkt):
        LOGGER.debug("<=== Received: %s", str(pkt))
        with self.__lock:
            handler = self.__handlers.get(pkt.Cmd, self._DefaultHandler)

        if (pkt.Cmd >> 8) == TYPE_ASYNC and pkt.Cmd != Packet.ASYNC_ACK:
            LOGGER.debug("Sending ACK packet for cmd %04X", pkt.Cmd)
            self._SendPacket(Packet.AsyncAck(pkt.Cmd))
        handler(pkt)

    def _Worker(self):
        try:
            s = b""
            while True:
                if self.__exit_event.isSet():
                    break

                s += self._ReadRawHID()
                # if s:
                #     LOGGER.info("Incoming buffer: %s", bytes_to_hex(s))

                # Look for the start of the next message, indicated by the magic bytes 0x55AA
                start = s.find(b"\x55\xAA")
                if start == -1:
                    time.sleep(0.1)
                    continue

                # Found the start of the next message, ideally this would be at the beginning of the buffer
                # but we could be tossing some bad data if a previous parse failed
                s = s[start:]
                LOGGER.debug("Trying to parse: %s", bytes_to_hex(s))
                try:
                    pkt = Packet.Parse(s)
                    if not pkt:
                        # Packet was invalid and couldn't be processed, remove the magic bytes and continue
                        # looking for another start of message. This essentially tosses the bad message.
                        LOGGER.error("Unable to parse message")
                        s = s[2:]
                        time.sleep(0.1)
                        continue
                except EOFError:
                    # Not enough data to parse a packet, keep the partial packet for now
                    time.sleep(0.1)
                    continue

                LOGGER.debug("Received: %s", bytes_to_hex(s[:pkt.Length]))
                s = s[pkt.Length:]
                self._HandlePacket(pkt)
        except Exception as e:
            LOGGER.error("Error occured in dongle worker thread", exc_info=True)
            self.__last_exception = e

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
        LOGGER.debug("Start Inquiry...")
        resp = self._DoSimpleCommand(Packet.Inquiry())

        assert len(resp.Payload) == 1
        result = resp.Payload[0]
        LOGGER.debug("Inquiry returns %d", result)

        assert result == 1, "Inquiry failed, result=%d" % result

    def _GetEnr(self, r):
        LOGGER.debug("Start GetEnr...")
        assert len(r) == 4
        assert all(isinstance(x, int) for x in r)
        r_string = bytes(struct.pack("<LLLL", *r))

        resp = self._DoSimpleCommand(Packet.GetEnr(r_string))
        assert len(resp.Payload) == 16
        LOGGER.debug("GetEnr returns %s", bytes_to_hex(resp.Payload))
        return resp.Payload

    def _GetMac(self):
        LOGGER.debug("Start GetMAC...")
        resp = self._DoSimpleCommand(Packet.GetMAC())
        assert len(resp.Payload) == 8
        mac = resp.Payload.decode('ascii')
        LOGGER.debug("GetMAC returns %s", mac)
        return mac

    def _GetKey(self):
        LOGGER.debug("Start GetKey...")
        resp = self._DoSimpleCommand(Packet.GetKey())
        assert len(resp.Payload) == 16
        LOGGER.debug("GetKey returns %s", resp.Payload)
        return resp.Payload

    def _GetVersion(self):
        LOGGER.debug("Start GetVersion...")
        resp = self._DoSimpleCommand(Packet.GetVersion())
        version = resp.Payload.decode('ascii')
        LOGGER.debug("GetVersion returns %s", version)
        return version

    def _GetSensorR1(self, mac, r1):
        LOGGER.info("Start GetSensorR1...")
        resp = self._DoSimpleCommand(Packet.GetSensorR1(mac, r1), 10)
        return resp.Payload

    def _EnableScan(self):
        LOGGER.info("Start EnableScan...")
        resp = self._DoSimpleCommand(Packet.EnableScan())
        assert len(resp.Payload) == 1
        result = resp.Payload[0]
        assert result == 0x01, "EnableScan failed, result=%d"

    def _DisableScan(self):
        LOGGER.info("Start DisableScan...")
        resp = self._DoSimpleCommand(Packet.DisableScan())
        assert len(resp.Payload) == 1
        result = resp.Payload[0]
        assert result == 0x01, "DisableScan failed, result=%d"

    def _GetSensors(self):
        LOGGER.info("Start GetSensors...")

        resp = self._DoSimpleCommand(Packet.GetSensorCount())
        assert len(resp.Payload) == 1
        count = resp.Payload[0]

        ctx = self.CmdContext(count=count, index=0, sensors=[])
        if count > 0:
            LOGGER.info("%d sensors reported, waiting for each one to report...", count)

            def cmd_handler(pkt, e):
                assert len(pkt.Payload) == 8
                mac = pkt.Payload.decode('ascii')
                LOGGER.info("Sensor %d/%d, MAC:%s", ctx.index + 1, ctx.count, mac)

                ctx.sensors.append(mac)
                ctx.index += 1
                if ctx.index == ctx.count:
                    e.set()

            self._DoCommand(Packet.GetSensorList(count), cmd_handler, timeout=10)
        else:
            LOGGER.info("No sensors bond yet...")
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
            LOGGER.info("Dongle MAC is [%s]", self.MAC)

            self.Version = self._GetVersion()
            LOGGER.info("Dongle version: %s", self.Version)

            self._FinishAuth()
        except:
            self.Stop()
            raise

    def List(self):
        sensors = self._GetSensors()
        return sensors
    
    def CheckError(self):
        if self.__last_exception:
            raise self.__last_exception

    def Stop(self, timeout=_CMD_TIMEOUT):
        self.__exit_event.set()
        self.__thread.join(timeout)
        os.close(self.__fd)
        self.__fd = None

    def Scan(self, timeout=60):
        LOGGER.info("Start Scan...")

        ctx = self.CmdContext(evt=threading.Event(), result=None)

        def scan_handler(pkt):
            assert len(pkt.Payload) == 11
            ctx.result = (pkt.Payload[1:9].decode('ascii'), pkt.Payload[9], pkt.Payload[10])
            ctx.evt.set()

        old_handler = self._SetHandler(Packet.NOTIFY_SENSOR_SCAN, scan_handler)
        try:
            self._DoSimpleCommand(Packet.EnableScan())

            if ctx.evt.wait(timeout):
                s_mac, s_type, s_ver = ctx.result
                LOGGER.info("Sensor found: mac=[%s], type=%d, version=%d", s_mac, s_type, s_ver)
                r1 = self._GetSensorR1(s_mac, b'Ok5HPNQ4lf77u754')
                LOGGER.debug("Sensor R1: %r", bytes_to_hex(r1))
            else:
                LOGGER.info("Sensor discovery timeout...")

            self._DoSimpleCommand(Packet.DisableScan())
        finally:
            self._SetHandler(Packet.NOTIFY_SENSOR_SCAN, old_handler)
        if ctx.result:
            s_mac, s_type, s_ver = ctx.result
            self._DoSimpleCommand(Packet.VerifySensor(s_mac), 10)
        return ctx.result

    def Delete(self, mac):
        resp = self._DoSimpleCommand(Packet.DelSensor(str(mac)))
        LOGGER.debug("CmdDelSensor returns %s", bytes_to_hex(resp.Payload))
        assert len(resp.Payload) == 9
        ack_mac = resp.Payload[:8].decode('ascii')
        ack_code = resp.Payload[8]
        assert ack_code == 0xFF, "CmdDelSensor: Unexpected ACK code: 0x%02X" % ack_code
        assert ack_mac == mac, "CmdDelSensor: MAC mismatch, requested:%s, returned:%s" % (mac, ack_mac)
        LOGGER.info("CmdDelSensor: %s deleted", mac)


def Open(device, event_handler, logger):
    global LOGGER
    if logger is not None:
       LOGGER = logger
    else:
       LOGGER = logging.getLogger(__name__)
    return Dongle(device, event_handler)
