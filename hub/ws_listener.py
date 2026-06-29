"""
WebSocket listener for incoming remote connections (hub side).

Accepts connections from ws2m-remote processes, validates the per-remote token
(or performs token-less adoption when pairing mode is active), collects any
replay frames, and hands off an authenticated RemoteTransport to the hub's
Bridge via the on_connection callback.

Auth sequence (hub side)
------------------------
1. Receive JSON auth message from remote:
       {"type": "auth", "remote_id": "...", "dongle_mac": "...",
        "fresh_start": bool, "queue_depth": N}
       Optional: "token": "..." if the remote has been previously adopted.

2a. If token present: look up remotes_path/<remote_id>/token and compare.
    If match → send auth_ok. If mismatch → send auth_fail and close.

2b. If no token and pairing active: generate token, save to
    remotes_path/<remote_id>/token, send {"type": "auth_token", "token": "..."},
    wait for {"type": "auth_ack"} from remote, then continue.

2c. If no token and pairing NOT active: send auth_fail reason=not_in_pairing_mode
    and close.

3. Send {"type": "auth_ok"} (skipped in adoption path — handled above).

4. If queue_depth > 0: receive exactly queue_depth binary frames, then
   receive {"type": "replay_done"}.

5. Call on_connection(transport) with the assembled RemoteTransport.
"""

import json
import logging
import pathlib
import secrets

import dongle_protocol
import websockets.sync.server


class WebSocketListener:
    """Runs a WebSocket server that authenticates and hands off remote connections.

    Each accepted connection is handled in its own thread by the websockets
    library; on_connection is called from that thread and is expected to return
    quickly (the DongleWorker it creates runs its own threads).
    """

    def __init__(
        self,
        port: int,
        remotes_path: pathlib.Path,
        get_pairing_active,
        on_connection,
        logger: logging.Logger,
    ):
        self._port = port
        self._remotes_path = remotes_path
        self._get_pairing_active = get_pairing_active
        self._on_connection = on_connection
        self._logger = logger.getChild("ws_listener")
        self._server: websockets.sync.server.WebSocketServer | None = None

    def serve_forever(self) -> None:
        """Block and serve incoming connections; call stop() to shut down."""
        with websockets.sync.server.serve(self._handle_connection, "0.0.0.0", self._port) as server:
            self._server = server
            self._logger.info(f"WebSocket remote listener started on port {self._port}")
            server.serve_forever()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

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

        if token_provided is not None:
            # Step 2a: token validation
            token_file = self._remotes_path / remote_id / "token"
            stored_token = None
            if token_file.exists():
                try:
                    stored_token = token_file.read_text().strip()
                except OSError:
                    pass
            if stored_token and token_provided == stored_token:
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
            try:
                token_dir.mkdir(parents=True, exist_ok=True)
                (token_dir / "token").write_text(new_token)
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
        transport = dongle_protocol.RemoteTransport(ws, remote_id, dongle_mac, replay_frames)
        self._on_connection(transport)
