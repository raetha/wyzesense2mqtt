"""
Integration tests for the adoption (pairing) protocol between
WebSocketListener._authenticate (hub-side) and Remote._authenticate (remote-side).

These tests exercise the full protocol exchange using a synchronous in-process
pipe — no real network is involved.  Each pair of tests drives hub and remote
over a bidirectional mock that passes messages between them synchronously,
verifying that both sides agree on tokens and produce the right outcomes.
"""

import json
import logging
import os
import pathlib
import sys
import threading
from queue import Empty, Queue

# hub/ modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub"))
# remote/ modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remote"))

_logger = logging.getLogger("test.adoption_integration")


# ---------------------------------------------------------------------------
# In-process bidirectional pipe (replaces a real WebSocket pair)
# ---------------------------------------------------------------------------


class _PipedConn:
    """One end of a synchronous in-process pipe used to simulate a WebSocket.

    Messages sent by this end are received by the ``other`` end, and vice versa.
    Both ends share a pair of queues.
    """

    def __init__(self, send_q: Queue, recv_q: Queue, name: str = ""):
        self._send_q = send_q
        self._recv_q = recv_q
        self._name = name
        self.remote_address = ("127.0.0.1", 12345)
        self._closed = False
        self._sent: list = []

    def send(self, msg) -> None:
        self._sent.append(msg)
        self._send_q.put(msg)

    def recv(self, timeout: float | None = None) -> object:
        try:
            return self._recv_q.get(timeout=timeout if timeout is not None else 5)
        except Empty:
            raise TimeoutError(f"{self._name}: recv() timed out") from None

    def close(self) -> None:
        self._closed = True

    @property
    def send_calls(self) -> list:
        return list(self._sent)


def _make_pipe() -> tuple["_PipedConn", "_PipedConn"]:
    """Return (hub_side, remote_side) bidirectional piped connection pair."""
    q_hub_to_remote: Queue = Queue()
    q_remote_to_hub: Queue = Queue()
    hub_side = _PipedConn(q_hub_to_remote, q_remote_to_hub, name="hub")
    remote_side = _PipedConn(q_remote_to_hub, q_hub_to_remote, name="remote")
    return hub_side, remote_side


# ---------------------------------------------------------------------------
# Helper to build hub listener and remote
# ---------------------------------------------------------------------------


def _build_hub(remotes_path: pathlib.Path, pairing_active: bool = False):
    from ws_listener import WebSocketListener

    on_conn_calls = []
    listener = WebSocketListener(
        port=8765,
        hub_id="test-hub-id",
        hub_version="4.0.0",
        remotes_path=remotes_path,
        get_pairing_active=lambda: pairing_active,
        on_connection=lambda t: on_conn_calls.append(t),
        logger=_logger,
    )
    return listener, on_conn_calls


def _build_remote(data_dir: pathlib.Path, remote_id: str = "test-remote-uuid", dongle_mac: str = "TESTMAC"):
    from remote import Remote

    r = Remote(
        hub_url="ws://127.0.0.1:8765",
        remote_id=remote_id,
        data_dir=data_dir,
        device="/dev/null",
    )
    r._dongle_mac = dongle_mac
    r._fresh_start = True
    return r


def _run_pipe_auth(listener, remote, hub_ws, ws_listener):
    """Run hub._authenticate and remote._authenticate concurrently over the piped WS pair."""
    errors: list = []

    def hub_side():
        try:
            listener._authenticate(hub_ws, "127.0.0.1:12345")
        except Exception as exc:
            errors.append(("hub", exc))

    def remote_side():
        try:
            remote._authenticate(ws_listener)
        except Exception as exc:
            errors.append(("remote", exc))

    t_hub = threading.Thread(target=hub_side, daemon=True)
    t_remote = threading.Thread(target=remote_side, daemon=True)
    t_hub.start()
    t_remote.start()
    t_hub.join(timeout=5)
    t_remote.join(timeout=5)
    return errors


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdoptionIntegration:
    def test_adoption_flow_end_to_end(self, tmp_path, monkeypatch):
        """Remote with no token connects to hub in pairing mode → token exchanged."""
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)

        remote_id = "integration-test-uuid"
        dongle_mac = "INTGTEST"
        hub_remotes = tmp_path / "remotes"
        remote_data = tmp_path / "remote_data"
        remote_data.mkdir()

        listener, on_conn_calls = _build_hub(hub_remotes, pairing_active=True)
        remote = _build_remote(remote_data, remote_id, dongle_mac)

        hub_ws, ws_listener = _make_pipe()
        errors = _run_pipe_auth(listener, remote, hub_ws, ws_listener)

        assert errors == [], f"Protocol errors: {errors}"

        # Hub must have saved the token
        hub_token_file = hub_remotes / remote_id / "token"
        assert hub_token_file.exists(), "Hub did not save token file"
        hub_token = hub_token_file.read_text().strip()
        assert len(hub_token) > 10

        # Remote must have saved the same token
        remote_token_file = remote_data / "hub_token"
        assert remote_token_file.exists(), "Remote did not save token file"
        remote_token = remote_token_file.read_text().strip()
        assert remote_token == hub_token

        # on_connection must have been called with the right transport
        assert len(on_conn_calls) == 1
        transport = on_conn_calls[0]
        assert transport.remote_id == remote_id
        assert transport.dongle_mac == dongle_mac

    def test_second_connect_with_token_succeeds(self, tmp_path, monkeypatch):
        """After adoption, remote uses saved token → hub authenticates and calls on_connection."""
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)

        remote_id = "reconnect-test-uuid"
        dongle_mac = "RCONNECT"
        token = "pre-shared-token-abc123"

        # Pre-populate hub side
        hub_remotes = tmp_path / "remotes"
        (hub_remotes / remote_id).mkdir(parents=True)
        (hub_remotes / remote_id / "token").write_text(token)

        # Pre-populate remote side
        remote_data = tmp_path / "remote_data"
        remote_data.mkdir()
        (remote_data / "hub_token").write_text(token)

        listener, on_conn_calls = _build_hub(hub_remotes, pairing_active=False)
        remote = _build_remote(remote_data, remote_id, dongle_mac)

        hub_ws, ws_listener = _make_pipe()
        errors = _run_pipe_auth(listener, remote, hub_ws, ws_listener)

        assert errors == [], f"Protocol errors: {errors}"
        assert len(on_conn_calls) == 1
        assert on_conn_calls[0].remote_id == remote_id

    def test_wrong_token_causes_auth_failure(self, tmp_path, monkeypatch):
        """Remote with wrong token → hub sends auth_fail → Remote raises RuntimeError."""
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)

        remote_id = "wrong-token-uuid"
        dongle_mac = "WRONGTOK"

        hub_remotes = tmp_path / "remotes"
        (hub_remotes / remote_id).mkdir(parents=True)
        (hub_remotes / remote_id / "token").write_text("correct-token")

        remote_data = tmp_path / "remote_data"
        remote_data.mkdir()
        (remote_data / "hub_token").write_text("wrong-token")

        listener, on_conn_calls = _build_hub(hub_remotes, pairing_active=False)
        remote = _build_remote(remote_data, remote_id, dongle_mac)

        hub_ws, ws_listener = _make_pipe()
        errors = _run_pipe_auth(listener, remote, hub_ws, ws_listener)

        # Hub side should raise ValueError (invalid token)
        hub_errors = [e for side, e in errors if side == "hub"]
        assert len(hub_errors) == 1
        assert isinstance(hub_errors[0], ValueError)

        # Remote side should raise RuntimeError (auth rejected)
        remote_errors = [e for side, e in errors if side == "remote"]
        assert len(remote_errors) == 1
        assert isinstance(remote_errors[0], RuntimeError)

        # on_connection must not have been called
        assert on_conn_calls == []

    def test_no_token_not_in_pairing_mode_rejected(self, tmp_path, monkeypatch):
        """Remote with no token, hub not in pairing mode → both sides get error."""
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)

        remote_data = tmp_path / "remote_data"
        remote_data.mkdir()
        hub_remotes = tmp_path / "remotes"

        listener, on_conn_calls = _build_hub(hub_remotes, pairing_active=False)
        remote = _build_remote(remote_data)

        hub_ws, ws_listener = _make_pipe()
        errors = _run_pipe_auth(listener, remote, hub_ws, ws_listener)

        hub_errors = [e for side, e in errors if side == "hub"]
        assert len(hub_errors) == 1
        assert isinstance(hub_errors[0], ValueError)

        remote_errors = [e for side, e in errors if side == "remote"]
        assert len(remote_errors) == 1
        assert isinstance(remote_errors[0], RuntimeError)

        assert on_conn_calls == []

    def test_hub_sends_auth_token_message_during_adoption(self, tmp_path, monkeypatch):
        """Hub sends an auth_token message that contains a non-empty token string."""
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)

        remote_data = tmp_path / "remote_data"
        remote_data.mkdir()
        hub_remotes = tmp_path / "remotes"

        listener, _ = _build_hub(hub_remotes, pairing_active=True)
        remote = _build_remote(remote_data)

        hub_ws, ws_listener = _make_pipe()
        errors = _run_pipe_auth(listener, remote, hub_ws, ws_listener)

        assert errors == []

        # Inspect what the hub sent — find the auth_token message
        hub_sent = [json.loads(m) for m in hub_ws.send_calls if isinstance(m, str)]
        auth_token_msgs = [m for m in hub_sent if m.get("type") == "auth_token"]
        assert len(auth_token_msgs) == 1
        assert len(auth_token_msgs[0].get("token", "")) > 10

    def test_auth_ok_message_sent_on_valid_token(self, tmp_path, monkeypatch):
        """Hub sends auth_ok after validating the correct token."""
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)

        remote_id = "auth-ok-test"
        token = "valid-token-abcdef"

        hub_remotes = tmp_path / "remotes"
        (hub_remotes / remote_id).mkdir(parents=True)
        (hub_remotes / remote_id / "token").write_text(token)

        remote_data = tmp_path / "remote_data"
        remote_data.mkdir()
        (remote_data / "hub_token").write_text(token)

        listener, _ = _build_hub(hub_remotes, pairing_active=False)
        remote = _build_remote(remote_data, remote_id)

        hub_ws, ws_listener = _make_pipe()
        errors = _run_pipe_auth(listener, remote, hub_ws, ws_listener)

        assert errors == []

        hub_sent = [json.loads(m) for m in hub_ws.send_calls if isinstance(m, str)]
        auth_ok_msgs = [m for m in hub_sent if m.get("type") == "auth_ok"]
        assert len(auth_ok_msgs) == 1
