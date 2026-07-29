"""
ws2m remote.

Holds any number of local USB WyzeSense dongles and forwards raw HID frames
to a ws2m hub over authenticated WebSocket connections.  The remote is
deliberately thin: it understands only GET_MAC (needed once per dongle to
learn its MAC address for the auth handshake) and forwards all other frames
opaque.

Architecture
------------
:class:`Remote` is a supervisor: it resolves the configured device selector
("auto", a directory of device nodes, or an explicit path) to concrete
devices and runs one :class:`DongleRelay` per dongle.  Each relay has its own
WebSocket connection, replay queue, and reconnect state — the hub sees each
dongle exactly as it would from a single-dongle remote, and one dongle's
failure never affects the others.

Per-relay startup sequence
--------------------------
1. Open the USB HID device.
2. Send GET_MAC (0x4304), read the response to obtain dongle_mac.
3. Enter the connection loop:
   a. Connect to the hub WebSocket.
   b. Send auth JSON; receive auth_ok or auth_token (adoption).
   c. Replay buffered frames (empty on first connect).
   d. Start two threads: dongle→hub and hub→dongle.
   e. On disconnect, wait with exponential backoff, then reconnect.

All relays share the remote_id and hub token.  When no token exists yet the
first relay is started alone so hub adoption happens exactly once; the rest
start as soon as the token is saved.

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
If a relay's HID device raises OSError it sends
{"type": "remote_unhealthy", "reason": "dongle_lost"} to the hub and enters a
reconnect loop (every 5 s, probing candidate paths by MAC since a replugged
dongle may come back under a different node name).  On success it sends
{"type": "remote_healthy"}.  The hub tracks these per dongle and publishes
the remote's aggregate health.

The container health file /tmp/ws2m_healthy is managed by the Remote
supervisor: present while the process runs and at least one relay has a
working dongle, removed when every dongle is lost (a container restart may
re-enumerate).  Per-dongle state is visible in HA regardless.
"""

import json
import logging
import os
import pathlib
import select
import threading
import time
import uuid

import websockets.exceptions
import websockets.sync.client
from device_discovery import find_all_dongle_devices, list_char_devices
from dongle_protocol import Packet
from frame_queue import FrameQueue, FrameType, InMemoryFrameQueue

# ---------------------------------------------------------------------------
# GET_MAC — the only protocol command the remote understands.
#
# Uses the shared dongle_protocol library for packet framing; everything else
# the remote relays is opaque bytes.
# ---------------------------------------------------------------------------

_GET_MAC_PACKET: bytes = Packet.get_mac().to_bytes()

# GET_MAC (0x4304) response is cmd 0x4305 (request + 1, per protocol convention)
_GET_MAC_RESPONSE_CMD: int = Packet.CMD_GET_MAC + 1

_HEALTH_FILE = pathlib.Path("/tmp/ws2m_healthy")  # noqa: S108


def _parse_mac_from_hid_frame(hid_frame: bytes) -> str | None:
    """Extract the 8-byte ASCII dongle MAC from a raw HID frame, or return None.

    The frame may contain multiple concatenated protocol packets; we walk the
    payload looking for the GET_MAC response packet.
    """
    if not hid_frame:
        return None
    length = hid_frame[0]
    if length < 1:
        return None
    data = hid_frame[1 : 1 + length]
    while len(data) >= 5:
        try:
            pkt = Packet.parse(data)
        except EOFError:
            break
        if pkt is None:
            break
        if pkt.cmd == _GET_MAC_RESPONSE_CMD and len(pkt.payload) >= 8:
            return pkt.payload[:8].decode("ascii", errors="replace").strip()
        data = data[pkt.length :]
    return None


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


def load_saved_setting(data_dir: pathlib.Path, name: str) -> str | None:
    """Return the persisted value of an HA-adjustable setting, or None.

    Settings changed from HA (via hub control frames) are saved to
    <data_dir>/<name> so they survive restarts.  Environment variables and
    CLI flags take precedence — a saved value is only used when neither is set.
    """
    setting_file = data_dir / name
    try:
        if setting_file.exists():
            value = setting_file.read_text().strip()
            return value or None
    except OSError:
        pass
    return None


def save_setting(data_dir: pathlib.Path, name: str, value: str, logger: logging.Logger) -> None:
    """Persist an HA-adjustable setting to <data_dir>/<name>."""
    setting_file = data_dir / name
    try:
        setting_file.parent.mkdir(parents=True, exist_ok=True)
        setting_file.write_text(value)
    except OSError as exc:
        logger.warning(f"Could not save setting {name!r}: {exc}")


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
# Per-dongle relay
# ---------------------------------------------------------------------------


class DongleRelay:
    """Forward one local WyzeSense USB dongle to a ws2m hub over WebSocket.

    A :class:`Remote` runs one relay per connected dongle; each relay has its
    own WebSocket connection, replay queue, and reconnect state, so dongles
    are fully independent of each other.

    Parameters
    ----------
    hub_url:
        Hub WebSocket URL, e.g. ``ws://192.168.1.10:8765``.
    remote_id:
        Stable UUID for this remote; shared by all relays of one remote.
    data_dir:
        Path to the data directory where hub_token is stored.
    device:
        Concrete HID device path (e.g. ``/dev/hidraw0``).  Selector values
        ("auto", directories) are resolved by :class:`Remote` before
        constructing relays.
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
    rediscover:
        Optional callable returning candidate device paths not currently
        held by sibling relays; used to find this relay's dongle again after
        a replug (the node name may have changed).
    logger:
        Parent logger; a per-device child is created internally.
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
        rediscover=None,
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
        self._rediscover = rediscover  # Callable[[], list[str]] | None
        self._logger = (logger or logging.getLogger("ws2m")).getChild(f"relay.{os.path.basename(device)}")

        self._fd: int | None = None
        self._dongle_mac: str | None = None
        self._fresh_start: bool = True
        self._stop: threading.Event = threading.Event()
        # True while the HID device is open and readable; Remote aggregates
        # these flags into the container-level health file.
        self.dongle_ok: bool = False
        # Set once the first auth round-trip succeeds — Remote uses it to
        # serialise hub adoption when no token exists yet.
        self.authenticated: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the relay.  Blocks until :meth:`stop` is called or interrupted."""
        self._open_hid()
        self._get_dongle_mac()
        self._logger.info(f"Dongle MAC={self._dongle_mac!r}  remote_id={self._remote_id!r}")
        self._connection_loop()

    def stop(self) -> None:
        """Signal the relay to exit gracefully."""
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
        """Save the hub token to <data_dir>/hub_token, owner-readable only."""
        token_file = self._token_file()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token)
        token_file.chmod(0o600)
        self._logger.info("Adopted by hub. Token saved to %s", token_file)

    # ------------------------------------------------------------------
    # HID device
    # ------------------------------------------------------------------

    def _open_hid(self) -> None:
        self._fd = os.open(self._device, os.O_RDWR)
        self.dongle_ok = True
        self._logger.debug(f"Opened HID device: {self._device}")

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
        self.authenticated.set()

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
                        self.dongle_ok = False
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
                                # The value is the device *selector* (auto / directory /
                                # path) applied at startup — persist it; the running
                                # relays keep their current dongles until restart.
                                value = str(parsed.get("value", "auto"))
                                self._logger.info(
                                    f"Dongle config updated to {value!r} by hub — effective after restart"
                                )
                                save_setting(self._data_dir, "dongle", value, self._logger)
                            elif msg_type == "set_log_level":
                                level = str(parsed.get("level", "INFO")).upper()
                                self._logger.info(f"Log level changed to {level} by hub")
                                logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
                                save_setting(self._data_dir, "log_level", level, self._logger)
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
        """Reopen this relay's dongle every 5 s until found or the WS drops.

        The original path is tried first.  If a rediscovery callback was
        provided, sibling-unclaimed candidate paths are then probed with
        GET_MAC — a replugged dongle usually comes back under a different
        node name, and the MAC is the relay's stable identity.
        """
        while not stop.is_set() and not self._stop.is_set():
            time.sleep(5)
            candidates = [self._device]
            if self._rediscover is not None:
                candidates += [p for p in self._rediscover() if p != self._device]
            for path in candidates:
                if self._try_reopen(path):
                    try:
                        ws.send(json.dumps({"type": "remote_healthy"}))
                    except Exception:
                        pass
                    return
            self._logger.debug(f"Dongle {self._dongle_mac} not found yet; retrying")

    def _try_reopen(self, path: str) -> bool:
        """Open *path* and adopt it if it answers GET_MAC with this relay's MAC."""
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return False
        old_fd, self._fd = self._fd, fd
        try:
            self._get_dongle_mac_probe()
        except (OSError, RuntimeError):
            os.close(fd)
            self._fd = old_fd
            return False
        self._device = path
        self.dongle_ok = True
        self._logger.info(f"Dongle {self._dongle_mac} reconnected: {path}")
        return True

    def _get_dongle_mac_probe(self) -> None:
        """GET_MAC round-trip that must match this relay's established MAC."""
        expected = self._dongle_mac
        self._get_dongle_mac()
        if expected is not None and self._dongle_mac != expected:
            self._dongle_mac = expected
            raise RuntimeError("Device answered with a different MAC — not this relay's dongle")


# ---------------------------------------------------------------------------
# Remote supervisor
# ---------------------------------------------------------------------------


class Remote:
    """Run one :class:`DongleRelay` per connected WyzeSense dongle.

    Parameters
    ----------
    hub_url, remote_id, data_dir, handshake_frame_count,
    reconnect_delay_initial, reconnect_delay_max:
        Passed through to every relay.
    device:
        Device selector: ``"auto"`` (all detected dongles), a directory of
        device nodes (all character devices inside), or an explicit
        ``/dev/hidrawN`` path.
    queue_factory:
        Zero-argument callable returning a fresh :class:`FrameQueue` for each
        relay.  Defaults to :class:`InMemoryFrameQueue` with its defaults.
    logger:
        Parent logger, shared with the relays.
    """

    # How long the first relay may take to obtain a token during adoption
    # before the remaining relays are started anyway (they will simply retry
    # auth until adoption completes).
    ADOPTION_WAIT_SECONDS = 300.0

    def __init__(
        self,
        *,
        hub_url: str,
        remote_id: str,
        data_dir: pathlib.Path,
        device: str = "auto",
        queue_factory=None,
        handshake_frame_count: int = 10,
        reconnect_delay_initial: float = 2.0,
        reconnect_delay_max: float = 60.0,
        logger: logging.Logger | None = None,
    ):
        self._hub_url = hub_url
        self._remote_id = remote_id
        self._data_dir = data_dir
        self._device = device
        self._queue_factory = queue_factory or InMemoryFrameQueue
        self._handshake_frame_count = handshake_frame_count
        self._reconnect_delay_initial = reconnect_delay_initial
        self._reconnect_delay_max = reconnect_delay_max
        self._logger = (logger or logging.getLogger("ws2m")).getChild("remote")

        self._relays: list[DongleRelay] = []
        self._stop: threading.Event = threading.Event()

    def _resolve_devices(self) -> list[str]:
        """Resolve the device selector to concrete /dev paths.

        "auto"          → every auto-detected WyzeSense dongle.
        "/dev/<dir>/"   → every character device inside the directory.
        "/dev/hidrawN"  → exactly this device.
        """
        device = self._device
        if device == "auto":
            devices = find_all_dongle_devices()
            if not devices:
                raise RuntimeError(
                    "No WyzeSense dongle found. Set WS2M_DONGLE=/dev/hidrawN or pass --dongle /dev/hidrawN."
                )
            self._logger.info(f"Auto-detected {len(devices)} dongle(s): {devices}")
            return devices
        if os.path.isdir(device):
            devices = list_char_devices(device)
            if not devices:
                raise RuntimeError(f"Dongle directory {device} contains no character devices.")
            self._logger.info(f"Using {len(devices)} dongle(s) from {device}: {devices}")
            return devices
        return [device]

    def _unclaimed_candidates(self) -> list[str]:
        """Candidate paths from the selector minus paths held by live relays.

        Given to each relay for post-replug rediscovery — a dongle often
        returns under a different node name, and probing a sibling's active
        device would inject a GET_MAC into its frame stream.
        """
        try:
            available = self._resolve_devices()
        except RuntimeError:
            return []
        claimed = {r._device for r in self._relays if r.dongle_ok}
        return [p for p in available if p not in claimed]

    def _make_relay(self, path: str) -> DongleRelay:
        return DongleRelay(
            hub_url=self._hub_url,
            remote_id=self._remote_id,
            data_dir=self._data_dir,
            device=path,
            queue=self._queue_factory(),
            handshake_frame_count=self._handshake_frame_count,
            reconnect_delay_initial=self._reconnect_delay_initial,
            reconnect_delay_max=self._reconnect_delay_max,
            rediscover=self._unclaimed_candidates,
            logger=self._logger,
        )

    def run(self) -> None:
        """Run relays for every resolved dongle.  Blocks until :meth:`stop`."""
        self._harden_existing_token_permissions()
        devices = self._resolve_devices()

        self._relays = [self._make_relay(path) for path in devices]

        threads: list[threading.Thread] = []

        def _start(relay: DongleRelay) -> None:
            t = threading.Thread(
                target=self._run_relay, args=(relay,), name=f"ws2m-relay-{os.path.basename(relay._device)}"
            )
            t.start()
            threads.append(t)

        # Serialise hub adoption: without a saved token, every relay would
        # race to be adopted in the pairing window.  Start one, wait for it
        # to authenticate (which saves the token), then start the rest.
        first, rest = self._relays[0], self._relays[1:]
        _start(first)
        if rest and self._read_token() is None:
            self._logger.info("No hub token yet — waiting for first dongle to be adopted before starting the rest")
            if not first.authenticated.wait(timeout=self.ADOPTION_WAIT_SECONDS):
                self._logger.warning("Adoption still pending — starting remaining relays; they will retry auth")
        for relay in rest:
            if self._stop.is_set():
                break
            _start(relay)

        try:
            self._health_loop()
        finally:
            for t in threads:
                t.join()

    def _run_relay(self, relay: DongleRelay) -> None:
        try:
            relay.run()
        except Exception:
            self._logger.error(f"Relay for {relay._device} exited with error", exc_info=True)
        finally:
            relay.dongle_ok = False

    def _harden_existing_token_permissions(self) -> None:
        """Fix permissions on a hub_token file saved before 4.0.1.

        Tokens are now written 0600 at save time, but that only covers fresh
        adoptions — a hub_token file saved under 4.0.0 has whatever
        permissions the process default left it with. Safe to call
        repeatedly (a no-op once already 0600) and if no file exists yet.
        """
        token_file = self._data_dir / "hub_token"
        try:
            if not token_file.is_file():
                return
            if (token_file.stat().st_mode & 0o777) != 0o600:
                token_file.chmod(0o600)
                self._logger.info(f"Fixed permissions on existing {token_file} (now 0600)")
        except OSError as exc:
            self._logger.warning(f"Could not fix permissions on {token_file}: {exc}")

    def _read_token(self) -> str | None:
        env_token = os.environ.get("WS2M_HUB_TOKEN")
        if env_token:
            return env_token
        token_file = self._data_dir / "hub_token"
        try:
            if token_file.exists():
                return token_file.read_text().strip() or None
        except OSError:
            pass
        return None

    def _health_loop(self) -> None:
        """Maintain the container health file until stopped.

        Present while at least one relay has a working dongle; removed when
        every dongle is lost, so a container restart (which re-enumerates
        devices) is suggested only when nothing is being relayed at all.
        """
        while not self._stop.is_set():
            if any(r.dongle_ok for r in self._relays):
                _HEALTH_FILE.touch()
            else:
                _HEALTH_FILE.unlink(missing_ok=True)
            self._stop.wait(10)
        _HEALTH_FILE.unlink(missing_ok=True)

    def stop(self) -> None:
        """Signal all relays and the health loop to exit gracefully."""
        self._stop.set()
        for relay in self._relays:
            relay.stop()
