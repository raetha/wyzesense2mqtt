"""
WebSocket server and mDNS advertisement for incoming remote connections.

WebSocketListener manages the hub side of the ws2m remote bridge protocol:

  - Runs a WebSocket server that accepts connections from ws2m-remote processes
  - Authenticates each connection (token validation or pairing-mode adoption)
  - Collects replay frames from the remote's ring buffer
  - Hands off an authenticated RemoteTransport to the hub's Bridge via the
    on_connection callback
  - Optionally advertises the hub via mDNS (_ws2m._tcp.local.) so remotes on
    the same network can discover the hub URL automatically

Auth sequence (hub side)
------------------------
1. Receive JSON auth message from remote:
       {"type": "auth", "remote_id": "...", "dongle_mac": "...",
        "fresh_start": bool, "queue_depth": N}
       Optional: "token": "..." if the remote has been previously adopted.

   remote_id is attacker-controlled (it arrives before any authentication),
   so it is validated against _SAFE_ID_RE before being used to build a
   filesystem path or any MQTT topic — otherwise a crafted value such as
   "../../etc" or one containing "/", "+", "#" could escape the intended
   token directory or inject extra MQTT topic levels / wildcards.

2a. If token present: look up remotes_path/<remote_id>/token and compare
    using a constant-time comparison (hmac.compare_digest) to avoid a
    timing side-channel on the token value.
    If match → send auth_ok. If mismatch → send auth_fail and close.

2b. If no token and pairing active: generate token, save to
    remotes_path/<remote_id>/token (mode 0600, readable only by the process
    owner), send {"type": "auth_token", "token": "..."}, wait for
    {"type": "auth_ack"} from remote, then continue.

2c. If no token and pairing NOT active: send auth_fail reason=not_in_pairing_mode
    and close.

3. Send {"type": "auth_ok"} (skipped in adoption path — handled above).

4. If queue_depth > 0: receive exactly queue_depth binary frames, then
   receive {"type": "replay_done"}.  queue_depth is capped at
   _MAX_REPLAY_FRAMES — an untrusted connection cannot claim an arbitrarily
   large depth and force the hub to block reading frames that never arrive.

5. Call on_connection(transport) with the assembled RemoteTransport.
"""

import hmac
import json
import logging
import pathlib
import re
import secrets
import threading

import websockets.sync.server
from bridge import RemoteTransport

# remote_id is attacker-controlled at the point it's first read (it arrives
# before any auth check), and it is used both as a filesystem path component
# (remotes_path/<remote_id>/token) and as an MQTT topic segment throughout
# bridge.py.  Restrict it to a safe, hostname-like charset so it can never
# contain a path separator, "..", or an MQTT wildcard ("+", "#").
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# Sane upper bound on a remote's claimed replay queue depth.  The remote's own
# ring buffer defaults to 500 frames; this is a generous ceiling so a
# misbehaving or malicious connection can't force the hub to block reading an
# unbounded number of frames (or accumulate unbounded memory) by claiming an
# enormous queue_depth.
_MAX_REPLAY_FRAMES = 10_000


def _is_safe_identifier(value: str) -> bool:
    """Return True if *value* is safe to use as a path component / topic segment.

    The regex alone isn't sufficient: "." and ".." are made up entirely of
    characters in the allowed set but are special path components (current /
    parent directory) even as a single segment, so they're rejected explicitly.
    """
    if value in (".", ".."):
        return False
    return bool(_SAFE_ID_RE.match(value))


class WebSocketListener:
    """WebSocket server and mDNS advertisement for ws2m remote connections.

    Lifecycle: call start() to begin accepting connections and optionally
    advertise via mDNS.  Call stop() to shut down both.  mDNS can be toggled
    independently while the server is running via start_mdns() / stop_mdns().
    """

    def __init__(
        self,
        port: int,
        hub_id: str,
        hub_version: str,
        remotes_path: pathlib.Path,
        get_pairing_active,
        on_connection,
        logger: logging.Logger,
    ):
        self._port = port
        self._hub_id = hub_id
        self._hub_version = hub_version
        self._remotes_path = remotes_path
        self._get_pairing_active = get_pairing_active
        self._on_connection = on_connection
        self._logger = logger.getChild("ws_listener")
        self._server: websockets.sync.server.WebSocketServer | None = None
        self._zeroconf = None
        self._zeroconf_lock = threading.Lock()

    # ------------------------------------------------------------------
    # One-time startup migration
    # ------------------------------------------------------------------

    def harden_existing_token_permissions(self) -> None:
        """Fix permissions on token files written before 4.0.1.

        Tokens are now written 0600 at creation time, but that only covers
        new adoptions — remotes adopted under 4.0.0 have token files with
        whatever permissions the process default left them with. Call this
        once at hub startup to bring existing files in line; safe to call
        repeatedly (a no-op once everything is already 0600), and safe if
        remotes_path doesn't exist yet (no remotes adopted).
        """
        if not self._remotes_path.is_dir():
            return
        fixed = 0
        for token_file in self._remotes_path.glob("*/token"):
            try:
                mode = token_file.stat().st_mode & 0o777
                if mode != 0o600:
                    token_file.chmod(0o600)
                    fixed += 1
            except OSError as exc:
                self._logger.warning(f"Could not fix permissions on {token_file}: {exc}")
        if fixed:
            self._logger.info(f"Fixed permissions on {fixed} existing remote token file(s) (now 0600)")

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def serve_forever(self) -> None:
        """Block and serve incoming connections; call stop() to shut down."""
        with websockets.sync.server.serve(self._handle_connection, "0.0.0.0", self._port) as server:
            self._server = server
            self._logger.info(f"WebSocket remote listener started on port {self._port}")
            server.serve_forever()

    def stop(self) -> None:
        """Stop the WebSocket server and any active mDNS advertisement."""
        self.stop_mdns()
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # ------------------------------------------------------------------
    # mDNS advertisement
    # ------------------------------------------------------------------

    def start_mdns(self) -> None:
        """Start mDNS advertisement for this hub.

        No-op if already advertising or if zeroconf is not installed.
        """
        with self._zeroconf_lock:
            if self._zeroconf is not None:
                return
            try:
                import socket

                import zeroconf as _zc

                svc_type = "_ws2m._tcp.local."
                svc_name = f"ws2m-hub-{self._hub_id}.{svc_type}"
                try:
                    local_ip = socket.gethostbyname(socket.gethostname())
                except OSError:
                    local_ip = "127.0.0.1"
                info = _zc.ServiceInfo(
                    svc_type,
                    svc_name,
                    addresses=[socket.inet_aton(local_ip)],
                    port=self._port,
                    properties={"hub_id": self._hub_id, "version": self._hub_version},
                )
                self._zeroconf = _zc.Zeroconf()
                self._zeroconf.register_service(info)
                self._logger.info(f"mDNS advertisement started: ws2m-hub-{self._hub_id}._ws2m._tcp.local.")
            except ImportError:
                self._logger.warning("zeroconf not installed — mDNS advertisement disabled.")

    def stop_mdns(self) -> None:
        """Stop mDNS advertisement if active.  No-op if not advertising."""
        with self._zeroconf_lock:
            if self._zeroconf is None:
                return
            try:
                self._zeroconf.unregister_all_services()
                self._zeroconf.close()
                self._logger.info("mDNS advertisement stopped")
            except Exception:
                pass
            self._zeroconf = None

    @property
    def mdns_active(self) -> bool:
        """True if mDNS advertisement is currently running."""
        with self._zeroconf_lock:
            return self._zeroconf is not None

    # ------------------------------------------------------------------
    # Per-connection handler (runs in websockets worker thread)
    # ------------------------------------------------------------------

    def _handle_connection(self, ws) -> None:
        peer = getattr(ws, "remote_address", "<unknown>")
        self._logger.debug(f"Incoming remote connection from {peer}")
        try:
            self._authenticate(ws, peer)
        except Exception as exc:
            self._logger.warning(f"Remote connection from {peer} failed: {exc}")
            try:
                ws.close()
            except Exception:
                pass

    def _authenticate(self, ws, peer) -> None:
        # Step 1: receive auth message (text JSON)
        raw = ws.recv(timeout=10)
        if not isinstance(raw, str):
            raise ValueError("Expected JSON auth message, got binary")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed auth JSON: {exc}") from exc

        if msg.get("type") != "auth":
            raise ValueError(f"Expected auth message, got type={msg.get('type')!r}")

        remote_id = str(msg.get("remote_id", peer))
        dongle_mac = str(msg.get("dongle_mac", ""))
        fresh_start = bool(msg.get("fresh_start", True))
        queue_depth = int(msg.get("queue_depth", 0))
        token_provided = msg.get("token")  # None if not present

        # remote_id gets used as a path component and an MQTT topic segment
        # below and throughout bridge.py — reject anything unsafe before any
        # of that happens, regardless of whether a token was provided.
        if not _is_safe_identifier(remote_id):
            ws.send(json.dumps({"type": "auth_fail", "reason": "invalid_remote_id"}))
            raise ValueError(f"Rejected unsafe remote_id={remote_id!r} from {peer}")

        if queue_depth < 0 or queue_depth > _MAX_REPLAY_FRAMES:
            ws.send(json.dumps({"type": "auth_fail", "reason": "invalid_queue_depth"}))
            raise ValueError(f"Rejected out-of-range queue_depth={queue_depth} from remote_id={remote_id!r}")

        if token_provided is not None:
            # Step 2a: token validation
            token_file = self._remotes_path / remote_id / "token"
            stored_token = None
            if token_file.exists():
                try:
                    stored_token = token_file.read_text().strip()
                except OSError:
                    pass
            # Constant-time comparison — a plain == would leak how many
            # leading characters matched via response timing.
            if stored_token and isinstance(token_provided, str) and hmac.compare_digest(token_provided, stored_token):
                self._logger.info(
                    f"Remote authenticated: remote_id={remote_id!r} dongle_mac={dongle_mac!r} "
                    f"fresh_start={fresh_start} queue_depth={queue_depth}"
                )
                ws.send(json.dumps({"type": "auth_ok"}))
            else:
                ws.send(json.dumps({"type": "auth_fail", "reason": "invalid_token"}))
                raise ValueError(f"Invalid token from remote_id={remote_id!r}")
        else:
            # Step 2b/2c: no token — check pairing mode
            if not self._get_pairing_active():
                ws.send(json.dumps({"type": "auth_fail", "reason": "not_in_pairing_mode"}))
                raise ValueError(f"Remote {remote_id!r} tried to adopt but pairing mode is not active")

            # Adoption: generate and save token
            new_token = secrets.token_urlsafe(32)
            token_dir = self._remotes_path / remote_id
            token_file = token_dir / "token"
            if token_file.exists():
                # Not blocked — a remote can legitimately lose its persisted
                # token (fresh container, wiped volume) and need to re-adopt
                # during a new pairing window — but this re-issues access
                # under an identity that was already adopted, so it's worth
                # a visible log line rather than a silent overwrite.
                self._logger.warning(
                    f"Re-adopting remote_id={remote_id!r} — a token already existed for this "
                    "identity and is being replaced. Expected if that remote lost its saved "
                    "token; unexpected otherwise."
                )
            try:
                token_dir.mkdir(parents=True, exist_ok=True)
                token_file.write_text(new_token)
                token_file.chmod(0o600)
            except OSError as exc:
                raise RuntimeError(f"Could not save token for remote {remote_id!r}: {exc}") from exc

            self._logger.info(f"Adopting remote {remote_id!r} — sending token")
            ws.send(json.dumps({"type": "auth_token", "token": new_token}))

            # Wait for auth_ack from remote
            ack_raw = ws.recv(timeout=15)
            if isinstance(ack_raw, str):
                try:
                    ack_msg = json.loads(ack_raw)
                except json.JSONDecodeError:
                    ack_msg = {}
                if ack_msg.get("type") != "auth_ack":
                    raise ValueError(f"Expected auth_ack from {remote_id!r}, got type={ack_msg.get('type')!r}")
            else:
                raise ValueError(f"Expected auth_ack text message from {remote_id!r}, got binary")

            self._logger.info(
                f"Remote adopted: remote_id={remote_id!r} dongle_mac={dongle_mac!r} "
                f"fresh_start={fresh_start} queue_depth={queue_depth}"
            )

        # Step 4: collect replay frames
        replay_frames: list[bytes] = []
        if queue_depth > 0:
            self._logger.debug(f"Collecting {queue_depth} replay frames from {remote_id!r}")
            for i in range(queue_depth):
                frame = ws.recv(timeout=30)
                if not isinstance(frame, bytes):
                    raise ValueError(f"Expected binary replay frame {i}, got text")
                replay_frames.append(frame)
            # receive replay_done
            done_raw = ws.recv(timeout=10)
            if isinstance(done_raw, str):
                try:
                    done_msg = json.loads(done_raw)
                except json.JSONDecodeError:
                    done_msg = {}
                if done_msg.get("type") != "replay_done":
                    self._logger.warning(f"Expected replay_done from {remote_id!r}, got type={done_msg.get('type')!r}")
            else:
                self._logger.warning(f"Expected replay_done text message from {remote_id!r}, got binary")

        # Step 5: build transport and hand off
        transport_obj = RemoteTransport(ws, remote_id, dongle_mac, replay_frames)
        self._on_connection(transport_obj)
