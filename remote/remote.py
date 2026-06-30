"""
ws2m remote.

Holds a local USB WyzeSense dongle and forwards raw HID frames to a ws2m hub
over an authenticated WebSocket connection.  The remote is deliberately thin:
it understands only GET_MAC (needed once to learn the dongle's MAC address for
the auth handshake) and forwards all other frames opaque.

Startup sequence
----------------
1. Open the USB HID device (auto-detected or explicit path).
2. Send GET_MAC (0x4304), read the response to obtain dongle_mac.
3. Enter the connection loop:
   a. Connect to the hub WebSocket.
   b. Send auth JSON; receive auth_ok or auth_token (adoption).
   c. Replay buffered frames (empty on first connect).
   d. Start two threads: dongle→hub and hub→dongle.
   e. On disconnect, wait with exponential backoff, then reconnect.

Ring buffer
-----------
Dongle→hub frames are accumulated in a FrameQueue while forwarding.  The
first ``handshake_frame_count`` frames are classified as "handshake" (always
retained); subsequent frames are "event" frames (TTL-limited).  On reconnect
the buffer is replayed before resuming live forwarding.

Identity and adoption
---------------------
The remote generates a UUID on first start and saves it to
<data_dir>/remote_id.  This UUID is the remote_id used in auth messages.

On first connect (no token): if the hub is in pairing mode, it sends an
auth_token message.  The remote saves the token to <data_dir>/hub_token and
sends auth_ack.  On subsequent connects the token is included in the auth
message for validation.

Health protocol
---------------
If the HID device raises OSError the remote:
1. Removes /tmp/ws2m_healthy (marks unhealthy).
2. Sends {"type": "remote_unhealthy", "reason": "dongle_lost"} to hub if connected.
3. Enters a reconnect loop (retries every 5 s).
4. On success: sends {"type": "remote_healthy"}, restores /tmp/ws2m_healthy.
"""

import json
import logging
import os
import pathlib
import select
import struct
import threading
import time
import uuid

import websockets.exceptions
import websockets.sync.client
from frame_queue import FrameQueue, FrameType, InMemoryFrameQueue

# ---------------------------------------------------------------------------
# Minimal inline packet builder — only GET_MAC is needed before hub connect
# ---------------------------------------------------------------------------


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFFFF


def _build_packet(cmd_type: int, cmd_id: int, payload: bytes = b"") -> bytes:
    """Serialise a WyzeSense protocol packet to wire bytes (no HID framing)."""
    hdr = struct.pack(">HB", 0xAA55, cmd_type) + struct.pack("BB", len(payload) + 3, cmd_id)
    body = hdr + payload
    return body + struct.pack(">H", _checksum(body))


# GET_MAC: sync (0x43), cmd_id 0x04, no payload → response cmd 0x43/0x05
_GET_MAC_PACKET: bytes = _build_packet(0x43, 0x04)

_HEALTH_FILE = pathlib.Path("/tmp/ws2m_healthy")  # noqa: S108


def _parse_mac_from_hid_frame(hid_frame: bytes) -> str | None:
    """Extract the 8-byte ASCII dongle MAC from a raw HID frame, or return None.

    The frame may contain multiple concatenated protocol packets; we walk the
    payload looking for a cmd 0x4305 (GET_MAC response) packet.
    """
    if not hid_frame:
        return None
    length = hid_frame[0]
    if length < 1:
        return None
    data = hid_frame[1 : 1 + length]
    while len(data) >= 7:
        magic = struct.unpack_from(">H", data)[0]
        if magic not in (0x55AA, 0xAA55):
            break
        cmd_type = data[2]
        b2 = data[3]
        cmd_id = data[4]
        pkt_end = b2 + 4
        if len(data) < pkt_end:
            break
        if cmd_type == 0x43 and cmd_id == 0x05:
            # Payload is data[5 : pkt_end - 2] (strip 2-byte checksum)
            payload = data[5 : pkt_end - 2]
            if len(payload) >= 8:
                return payload[:8].decode("ascii", errors="replace").strip()
        data = data[pkt_end:]
    return None


# ---------------------------------------------------------------------------
# Device auto-detection
# ---------------------------------------------------------------------------


def _find_dongle_devices() -> list[str]:
    """Scan /sys/class/hidraw for WyzeSense dongles (USB vendor 1a86, product e024)."""
    import subprocess

    devices: list[str] = []
    try:
        raw = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode().lower()
        for line in raw.splitlines():
            if "e024" in line and "1a86" in line:
                for part in line.split():
                    if "hidraw" in part:
                        devices.append(f"/dev/{part}")
                        break
    except (subprocess.CalledProcessError, OSError):
        pass
    return devices


# ---------------------------------------------------------------------------
# Remote identity helpers
# ---------------------------------------------------------------------------


def _load_or_create_remote_id(data_dir: pathlib.Path) -> str:
    """Return the stable remote UUID, generating and persisting it if needed.

    The UUID is saved to <data_dir>/remote_id on first run.
    """
    id_file = data_dir / "remote_id"
    if id_file.exists():
        return id_file.read_text().strip()
    remote_id = str(uuid.uuid4())
    id_file.parent.mkdir(parents=True, exist_ok=True)
    id_file.write_text(remote_id)
    return remote_id


# ---------------------------------------------------------------------------
# mDNS discovery
# ---------------------------------------------------------------------------


def _discover_hub_via_mdns(
    timeout: float = 30.0,
    preferred_hub_id: str | None = None,
    logger: logging.Logger | None = None,
) -> str | None:
    """Discover the ws2m hub WebSocket URL via mDNS.

    Browses for _ws2m._tcp.local. services and returns the first matching
    ws://<address>:<port> URL.  If preferred_hub_id is set, prefers the service
    whose hub_id property matches; otherwise returns the first service found.

    Returns None on timeout or if zeroconf is not installed.
    """
    log = logger or logging.getLogger("ws2m.remote")
    try:
        import ipaddress
        import socket

        import zeroconf as _zc
        from zeroconf import ServiceBrowser, ServiceStateChange
    except ImportError:
        log.error("zeroconf not installed — cannot discover hub via mDNS")
        return None

    SERVICE_TYPE = "_ws2m._tcp.local."
    found_event = threading.Event()
    found_url: list[str] = []

    def _on_service_state_change(zeroconf_inst, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added:
            return
        info = zeroconf_inst.get_service_info(service_type, name)
        if info is None:
            return
        props = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in (info.properties or {}).items()
        }
        hub_id_prop = props.get("hub_id", "")
        if preferred_hub_id and hub_id_prop != preferred_hub_id:
            log.debug(f"Skipping hub {hub_id_prop!r} (looking for {preferred_hub_id!r})")
            return
        # Resolve address
        if info.addresses:
            try:
                addr = str(ipaddress.ip_address(info.addresses[0]))
            except ValueError:
                try:
                    addr = socket.inet_ntoa(info.addresses[0])
                except OSError:
                    addr = info.server.rstrip(".")
        else:
            addr = info.server.rstrip(".")
        url = f"ws://{addr}:{info.port}"
        log.info(f"Found hub at {url}")
        found_url.append(url)
        found_event.set()

    zc = _zc.Zeroconf()
    try:
        _browser = ServiceBrowser(zc, SERVICE_TYPE, handlers=[_on_service_state_change])
        found_event.wait(timeout=timeout)
    finally:
        zc.close()

    return found_url[0] if found_url else None


# ---------------------------------------------------------------------------
# Remote
# ---------------------------------------------------------------------------


class Remote:
    """Forward a local WyzeSense USB dongle to a ws2m hub over WebSocket.

    Parameters
    ----------
    hub_url:
        Hub WebSocket URL, e.g. ``ws://192.168.1.10:8765``.
    remote_id:
        Stable UUID for this remote; persisted to <data_dir>/remote_id.
    data_dir:
        Path to the data directory where hub_token is stored.
    device:
        HID device path (e.g. ``/dev/hidraw0``) or ``"auto"`` to
        auto-detect the first connected WyzeSense dongle.
    queue:
        Ring buffer for replay frames.  Defaults to
        :class:`InMemoryFrameQueue` with ``max_seconds=10, max_frames=500``.
    handshake_frame_count:
        Number of dongle→hub frames to classify as "handshake" (always
        retained in the replay buffer).  Default 10 comfortably covers the
        hub's 5-step init sequence including async ACKs.
    reconnect_delay_initial:
        Starting backoff delay in seconds (default 2).
    reconnect_delay_max:
        Maximum backoff delay in seconds (default 60).
    logger:
        Parent logger; a ``remote`` child is created internally.
    """

    def __init__(
        self,
        *,
        hub_url: str,
        remote_id: str,
        data_dir: pathlib.Path,
        device: str = "auto",
        queue: FrameQueue | None = None,
        handshake_frame_count: int = 10,
        reconnect_delay_initial: float = 2.0,
        reconnect_delay_max: float = 60.0,
        logger: logging.Logger | None = None,
    ):
        self._hub_url = hub_url
        self._remote_id = remote_id
        self._data_dir = data_dir
        self._device = device
        self._queue = queue if queue is not None else InMemoryFrameQueue()
        self._handshake_frame_count = handshake_frame_count
        self._reconnect_delay_initial = reconnect_delay_initial
        self._reconnect_delay_max = reconnect_delay_max
        self._logger = (logger or logging.getLogger("ws2m")).getChild("remote")

        self._fd: int | None = None
        self._dongle_mac: str | None = None
        self._fresh_start: bool = True
        self._stop: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the remote.  Blocks until :meth:`stop` is called or interrupted."""
        self._open_hid()
        self._get_dongle_mac()
        self._logger.info(f"Dongle MAC={self._dongle_mac!r}  remote_id={self._remote_id!r}")
        self._connection_loop()

    def stop(self) -> None:
        """Signal the remote to exit gracefully."""
        self._stop.set()
        fd = self._fd
        if fd is not None:
            self._fd = None
            try:
                os.close(fd)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _token_file(self) -> pathlib.Path:
        return self._data_dir / "hub_token"

    def _read_token(self) -> str | None:
        """Return the hub token, checking WS2M_HUB_TOKEN env var first."""
        env_token = os.environ.get("WS2M_HUB_TOKEN")
        if env_token:
            return env_token
        token_file = self._token_file()
        if token_file.exists():
            try:
                return token_file.read_text().strip() or None
            except OSError:
                return None
        return None

    def _save_token(self, token: str) -> None:
        """Save the hub token to <data_dir>/hub_token."""
        token_file = self._token_file()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token)
        self._logger.info("Adopted by hub. Token saved to %s", token_file)

    # ------------------------------------------------------------------
    # HID device
    # ------------------------------------------------------------------

    def _open_hid(self) -> None:
        device = self._device
        if device == "auto":
            devices = _find_dongle_devices()
            if not devices:
                raise RuntimeError(
                    "No WyzeSense dongle found. Set WS2M_DONGLE=/dev/hidrawN or pass --dongle /dev/hidrawN."
                )
            device = devices[0]
            self._logger.info(f"Auto-detected dongle: {device}")
        self._fd = os.open(device, os.O_RDWR)
        self._logger.debug(f"Opened HID device: {device}")

    def _read_hid_frame(self, timeout: float = 1.0) -> bytes | None:
        """Read one 64-byte HID report, returning None on timeout."""
        if self._fd is None:
            raise RuntimeError("HID device is not open")
        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return None
        return os.read(self._fd, 0x40)

    def _write_hid(self, data: bytes) -> None:
        if self._fd is None:
            raise RuntimeError("HID device is not open")
        os.write(self._fd, data)

    def _get_dongle_mac(self) -> None:
        """Send GET_MAC, read the response, store in ``self._dongle_mac``."""
        self._logger.debug("Sending GET_MAC to dongle")
        self._write_hid(_GET_MAC_PACKET)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            frame = self._read_hid_frame(timeout=min(1.0, deadline - time.monotonic()))
            if frame is None:
                continue
            mac = _parse_mac_from_hid_frame(frame)
            if mac:
                self._dongle_mac = mac
                return
            self._logger.debug(f"Pre-MAC frame (discarded): {frame[:8].hex()!r}")
        raise RuntimeError("GET_MAC timed out — is the dongle connected?")

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    def _connection_loop(self) -> None:
        delay = self._reconnect_delay_initial
        while not self._stop.is_set():
            try:
                self._logger.info(f"Connecting to hub: {self._hub_url}")
                self._connect_and_forward()
                delay = self._reconnect_delay_initial  # reset after clean session
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._logger.warning(f"Connection lost: {exc}  (retry in {delay:.0f}s)")
            self._stop.wait(delay)
            delay = min(delay * 2, self._reconnect_delay_max)

    def _connect_and_forward(self) -> None:
        with websockets.sync.client.connect(self._hub_url, open_timeout=10) as ws:
            self._authenticate(ws)
            self._logger.info("Authenticated; forwarding frames")
            self._bidirectional_forward(ws)

    # ------------------------------------------------------------------
    # Auth + replay
    # ------------------------------------------------------------------

    def _authenticate(self, ws) -> None:
        """Send auth, handle auth_ok/auth_token/auth_fail, replay buffered frames."""
        replay_frames: list[bytes] = [] if self._fresh_start else self._queue.get_replay_frames()

        token = self._read_token()
        auth_msg: dict = {
            "type": "auth",
            "remote_id": self._remote_id,
            "dongle_mac": self._dongle_mac,
            "fresh_start": self._fresh_start,
            "queue_depth": len(replay_frames),
        }
        if token is not None:
            auth_msg["token"] = token

        ws.send(json.dumps(auth_msg))

        raw = ws.recv(timeout=10)
        if not isinstance(raw, str):
            raise ValueError("Expected JSON auth response, got binary")
        resp = json.loads(raw)

        if resp.get("type") == "auth_fail":
            raise RuntimeError(f"Hub rejected auth: {resp.get('reason', 'unknown')}")

        if resp.get("type") == "auth_token":
            # Adoption flow: hub sent us a token
            new_token = resp.get("token", "")
            if not new_token:
                raise ValueError("auth_token message missing token field")
            self._save_token(new_token)
            ws.send(json.dumps({"type": "auth_ack"}))
            self._logger.info("Adoption complete")
        elif resp.get("type") != "auth_ok":
            raise ValueError(f"Unexpected auth response type={resp.get('type')!r}")

        if replay_frames:
            self._logger.debug(f"Replaying {len(replay_frames)} frames")
            for frame in replay_frames:
                ws.send(frame)
            ws.send(json.dumps({"type": "replay_done"}))

        self._fresh_start = False

    # ------------------------------------------------------------------
    # Bidirectional forwarding
    # ------------------------------------------------------------------

    def _bidirectional_forward(self, ws) -> None:
        """Run two threads forwarding frames in both directions until one fails.

        dongle_reader  — reads HID frames → ring buffer → hub WebSocket
        hub_reader     — reads hub WebSocket messages → HID device

        If the dongle raises OSError, sends remote_unhealthy to hub and enters
        a dongle reconnect loop.  When reconnected, sends remote_healthy.
        """
        stop = threading.Event()
        frames_forwarded = [0]  # mutable int for handshake classification

        def dongle_reader() -> None:
            try:
                while not stop.is_set() and not self._stop.is_set():
                    try:
                        frame = self._read_hid_frame(timeout=1.0)
                    except OSError as exc:
                        self._logger.error(f"Dongle read error: {exc} — entering reconnect loop")
                        _HEALTH_FILE.unlink(missing_ok=True)
                        # Notify hub
                        try:
                            ws.send(json.dumps({"type": "remote_unhealthy", "reason": "dongle_lost"}))
                        except Exception:
                            pass
                        # Reconnect loop
                        self._dongle_reconnect_loop(ws, stop)
                        break
                    if frame is None:
                        continue
                    count = frames_forwarded[0]
                    frame_type: FrameType = "handshake" if count < self._handshake_frame_count else "event"
                    frames_forwarded[0] = count + 1
                    self._queue.push(frame, frame_type)
                    ws.send(frame)
            except Exception as exc:
                self._logger.debug(f"dongle_reader exiting: {exc}")
            finally:
                stop.set()

        def hub_reader() -> None:
            try:
                while not stop.is_set() and not self._stop.is_set():
                    try:
                        msg = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    if isinstance(msg, bytes):
                        try:
                            self._write_hid(msg)
                        except OSError:
                            pass
                    else:
                        # Text control messages from the hub
                        try:
                            parsed = json.loads(msg)
                            msg_type = parsed.get("type")
                            if msg_type == "restart":
                                self._logger.warning("Restart requested by hub — shutting down cleanly")
                                _HEALTH_FILE.unlink(missing_ok=True)
                                self.stop()
                                os._exit(0)
                            elif msg_type == "set_dongle":
                                value = str(parsed.get("value", "auto"))
                                self._logger.info(
                                    f"Dongle config updated to {value!r} by hub — effective after restart"
                                )
                                self._device = value
                            elif msg_type == "set_log_level":
                                level = str(parsed.get("level", "INFO")).upper()
                                self._logger.info(f"Log level changed to {level} by hub")
                                logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
                            else:
                                self._logger.debug(f"Unexpected control message from hub: {msg[:80]!r}")
                        except Exception:
                            self._logger.debug(f"Unexpected text from hub: {msg[:80]!r}")
            except websockets.exceptions.ConnectionClosed:
                self._logger.debug("Hub WebSocket closed")
            except Exception as exc:
                self._logger.debug(f"hub_reader exiting: {exc}")
            finally:
                stop.set()

        t_dongle = threading.Thread(target=dongle_reader, daemon=True, name="ws2m-dongle-reader")
        t_hub = threading.Thread(target=hub_reader, daemon=True, name="ws2m-hub-reader")
        t_dongle.start()
        t_hub.start()
        t_dongle.join()
        t_hub.join()
        self._logger.debug("Bidirectional forwarding ended")

    def _dongle_reconnect_loop(self, ws, stop: threading.Event) -> None:
        """Try to reopen the HID device every 5 s until success or WS drops."""
        device = self._device
        while not stop.is_set() and not self._stop.is_set():
            time.sleep(5)
            try:
                if device == "auto":
                    devices = _find_dongle_devices()
                    if not devices:
                        self._logger.debug("Dongle reconnect: no device found yet")
                        continue
                    device_path = devices[0]
                else:
                    device_path = device
                fd = os.open(device_path, os.O_RDWR)
                self._fd = fd
                self._logger.info(f"Dongle reconnected: {device_path}")
                _HEALTH_FILE.touch()
                try:
                    ws.send(json.dumps({"type": "remote_healthy"}))
                except Exception:
                    pass
                return
            except OSError as exc:
                self._logger.debug(f"Dongle reconnect failed: {exc}")
