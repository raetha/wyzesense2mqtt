"""
Tests for ws_listener.WebSocketListener.

The WebSocketListener authenticates incoming remote connections and
hands off a RemoteTransport to a callback.  All tests use a simple
mock WebSocket object — no real network is involved.
"""

import json
import logging
import pathlib
import tempfile
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_logger = logging.getLogger("test.ws_listener")


def _make_ws(recv_sequence: list) -> MagicMock:
    """Build a mock WebSocket whose recv() yields items from recv_sequence."""
    ws = MagicMock()
    ws.recv.side_effect = list(recv_sequence)
    ws.send = MagicMock()
    ws.close = MagicMock()
    ws.remote_address = ("127.0.0.1", 54321)
    return ws


def _auth_msg(
    remote_id: str = "test-remote-uuid",
    dongle_mac: str = "AABBCCDD",
    fresh_start: bool = True,
    queue_depth: int = 0,
    token: str | None = None,
) -> str:
    msg: dict = {
        "type": "auth",
        "remote_id": remote_id,
        "dongle_mac": dongle_mac,
        "fresh_start": fresh_start,
        "queue_depth": queue_depth,
    }
    if token is not None:
        msg["token"] = token
    return json.dumps(msg)


def _build_listener(
    remotes_path: pathlib.Path | None = None,
    get_pairing_active=None,
    on_connection=None,
    tmp_dir: pathlib.Path | None = None,
):
    from ws_listener import WebSocketListener

    if remotes_path is None:
        # Use a temp dir if not provided
        _tmp = tempfile.mkdtemp()
        remotes_path = pathlib.Path(_tmp)
    if get_pairing_active is None:

        def get_pairing_active():
            return False

    if on_connection is None:
        on_connection = MagicMock()
    listener = WebSocketListener(
        port=8765,
        remotes_path=remotes_path,
        get_pairing_active=get_pairing_active,
        on_connection=on_connection,
        logger=_logger,
    )
    return listener, on_connection


def _build_listener_with_stored_token(token: str, remote_id: str = "test-remote-uuid"):
    """Build a listener that has a pre-stored token for the given remote_id."""
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    remotes_path = tmp / "remotes"
    token_dir = remotes_path / remote_id
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "token").write_text(token)
    listener, on_conn = _build_listener(remotes_path=remotes_path)
    return listener, on_conn, remotes_path


# ---------------------------------------------------------------------------
# Successful authentication — with token
# ---------------------------------------------------------------------------


class TestAuthSuccessWithToken:
    def test_auth_ok_sent_on_valid_token(self):
        token = "my-valid-token"
        remote_id = "test-remote-uuid"
        listener, on_conn, _ = _build_listener_with_stored_token(token, remote_id)
        ws = _make_ws([_auth_msg(token=token, remote_id=remote_id)])
        listener._authenticate(ws, "127.0.0.1:1234")

        text_sends = [c.args[0] for c in ws.send.call_args_list if isinstance(c.args[0], str)]
        assert any('"auth_ok"' in m for m in text_sends)

    def test_on_connection_called_with_remote_transport(self):
        from dongle_protocol import RemoteTransport

        token = "my-valid-token"
        remote_id = "test-uuid-abc"
        listener, on_conn, _ = _build_listener_with_stored_token(token, remote_id)
        ws = _make_ws([_auth_msg(token=token, remote_id=remote_id, dongle_mac="DEADBEEF")])
        listener._authenticate(ws, "127.0.0.1:1234")

        on_conn.assert_called_once()
        transport = on_conn.call_args.args[0]
        assert isinstance(transport, RemoteTransport)
        assert transport.dongle_mac == "DEADBEEF"
        assert transport.remote_id == remote_id

    def test_no_replay_when_queue_depth_zero(self):
        token = "my-valid-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)
        ws = _make_ws([_auth_msg(token=token, queue_depth=0)])
        listener._authenticate(ws, "127.0.0.1:1234")

        binary_sends = [c.args[0] for c in ws.send.call_args_list if isinstance(c.args[0], bytes)]
        assert binary_sends == []


# ---------------------------------------------------------------------------
# Token validation failures
# ---------------------------------------------------------------------------


class TestInvalidToken:
    def test_auth_fail_sent_on_wrong_token(self):
        token = "correct-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)
        ws = _make_ws([_auth_msg(token="wrong-token")])
        with pytest.raises(ValueError, match="Invalid token"):
            listener._authenticate(ws, "127.0.0.1:1234")

        text_sends = [c.args[0] for c in ws.send.call_args_list if isinstance(c.args[0], str)]
        assert any('"auth_fail"' in m for m in text_sends)

    def test_on_connection_not_called_on_wrong_token(self):
        token = "correct-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)
        ws = _make_ws([_auth_msg(token="wrong-token")])
        with pytest.raises(ValueError):
            listener._authenticate(ws, "127.0.0.1:1234")
        on_conn.assert_not_called()

    def test_auth_fail_when_no_stored_token_but_token_provided(self):
        """Providing a token when no stored token exists → auth_fail."""
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp())
        remotes_path = tmp / "remotes"
        listener, on_conn = _build_listener(remotes_path=remotes_path)
        ws = _make_ws([_auth_msg(token="some-token")])
        with pytest.raises(ValueError, match="Invalid token"):
            listener._authenticate(ws, "127.0.0.1:1234")


# ---------------------------------------------------------------------------
# Adoption (pairing mode) flow
# ---------------------------------------------------------------------------


class TestAdoptionFlow:
    def test_adoption_when_pairing_active(self):
        """No token + pairing active → auth_token sent, ack received, on_connection called."""
        import tempfile

        from dongle_protocol import RemoteTransport

        tmp = pathlib.Path(tempfile.mkdtemp())
        remotes_path = tmp / "remotes"
        listener, on_conn = _build_listener(
            remotes_path=remotes_path,
            get_pairing_active=lambda: True,
        )
        ws = _make_ws(
            [
                _auth_msg(token=None),  # no token
                json.dumps({"type": "auth_ack"}),  # remote acknowledges token
            ]
        )
        listener._authenticate(ws, "127.0.0.1:1234")

        # auth_token must have been sent
        text_sends = [c.args[0] for c in ws.send.call_args_list if isinstance(c.args[0], str)]
        assert any('"auth_token"' in m for m in text_sends)

        # Token file must have been saved
        token_file = remotes_path / "test-remote-uuid" / "token"
        assert token_file.exists()
        assert len(token_file.read_text().strip()) > 10

        # on_connection should be called with a RemoteTransport
        on_conn.assert_called_once()
        transport = on_conn.call_args.args[0]
        assert isinstance(transport, RemoteTransport)

    def test_rejection_when_not_in_pairing_mode(self):
        """No token + pairing inactive → auth_fail reason=not_in_pairing_mode."""
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp())
        remotes_path = tmp / "remotes"
        listener, on_conn = _build_listener(
            remotes_path=remotes_path,
            get_pairing_active=lambda: False,
        )
        ws = _make_ws([_auth_msg(token=None)])
        with pytest.raises(ValueError, match="not_in_pairing_mode|pairing mode"):
            listener._authenticate(ws, "127.0.0.1:1234")

        text_sends = [c.args[0] for c in ws.send.call_args_list if isinstance(c.args[0], str)]
        assert any("not_in_pairing_mode" in m for m in text_sends)
        on_conn.assert_not_called()

    def test_second_connect_with_adopted_token(self):
        """After adoption, a subsequent connect with the saved token succeeds."""
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp())
        remotes_path = tmp / "remotes"
        remote_id = "test-remote-uuid"
        listener, on_conn = _build_listener(
            remotes_path=remotes_path,
            get_pairing_active=lambda: True,
        )

        # First connect: adopt
        ws1 = _make_ws(
            [
                _auth_msg(remote_id=remote_id, token=None),
                json.dumps({"type": "auth_ack"}),
            ]
        )
        listener._authenticate(ws1, "127.0.0.1:1234")

        # Read the saved token
        saved_token = (remotes_path / remote_id / "token").read_text().strip()

        # Second connect: use saved token → auth_ok
        listener2, on_conn2 = _build_listener(
            remotes_path=remotes_path,
            get_pairing_active=lambda: False,  # pairing inactive now
        )
        ws2 = _make_ws([_auth_msg(remote_id=remote_id, token=saved_token)])
        listener2._authenticate(ws2, "127.0.0.1:1234")

        text_sends = [c.args[0] for c in ws2.send.call_args_list if isinstance(c.args[0], str)]
        assert any('"auth_ok"' in m for m in text_sends)
        on_conn2.assert_called_once()


# ---------------------------------------------------------------------------
# Replay frames
# ---------------------------------------------------------------------------


class TestReplayFrames:
    def _make_binary_frame(self, b: int) -> bytes:
        return bytes([b]) * 64

    def test_replay_frames_received_before_replay_done(self):
        """With queue_depth=2, the listener reads 2 binary frames then replay_done."""
        from dongle_protocol import RemoteTransport

        token = "my-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)

        frame1 = self._make_binary_frame(0x01)
        frame2 = self._make_binary_frame(0x02)
        recv_seq = [
            _auth_msg(token=token, queue_depth=2),
            frame1,
            frame2,
            json.dumps({"type": "replay_done"}),
        ]
        ws = _make_ws(recv_seq)
        listener._authenticate(ws, "127.0.0.1:1234")

        transport: RemoteTransport = on_conn.call_args.args[0]
        # Replay frames should be present in the transport's deque
        assert transport.read() == frame1
        assert transport.read() == frame2

    def test_non_binary_replay_frame_raises(self):
        """If a replay slot contains text (not bytes), authenticate raises ValueError."""
        token = "my-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)
        recv_seq = [_auth_msg(token=token, queue_depth=1), "oops this is text"]
        ws = _make_ws(recv_seq)
        with pytest.raises(ValueError, match="binary replay frame"):
            listener._authenticate(ws, "127.0.0.1:1234")


# ---------------------------------------------------------------------------
# Malformed auth messages
# ---------------------------------------------------------------------------


class TestMalformedAuth:
    def test_non_json_raises(self):
        listener, _ = _build_listener()
        ws = _make_ws(["not json }{"])
        with pytest.raises(ValueError, match="Malformed auth JSON"):
            listener._authenticate(ws, "127.0.0.1:1234")

    def test_wrong_type_raises(self):
        listener, _ = _build_listener()
        ws = _make_ws([json.dumps({"type": "hello"})])
        with pytest.raises(ValueError, match="Expected auth message"):
            listener._authenticate(ws, "127.0.0.1:1234")

    def test_binary_first_message_raises(self):
        listener, _ = _build_listener()
        ws = _make_ws([b"\xaa\x55\x43\x03\x04\x01\x49"])
        with pytest.raises(ValueError, match="Expected JSON auth message"):
            listener._authenticate(ws, "127.0.0.1:1234")


# ---------------------------------------------------------------------------
# _handle_connection — exception handling
# ---------------------------------------------------------------------------


class TestHandleConnection:
    def test_handle_connection_closes_ws_on_auth_failure(self):
        """_handle_connection must close the WS and not re-raise on auth errors."""
        listener, _ = _build_listener(get_pairing_active=lambda: False)
        ws = _make_ws([_auth_msg(token=None)])
        listener._handle_connection(ws)  # must not raise
        ws.close.assert_called()

    def test_handle_connection_does_not_close_on_success(self):
        """On success the WS is handed off; _handle_connection should not close it."""
        token = "my-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)
        ws = _make_ws([_auth_msg(token=token)])
        on_conn.return_value = None
        listener._handle_connection(ws)
        ws.close.assert_not_called()

    def test_handle_connection_close_exception_is_swallowed(self):
        """If ws.close() raises during auth failure cleanup, _handle_connection must not re-raise."""
        listener, _ = _build_listener(get_pairing_active=lambda: False)
        ws = _make_ws([_auth_msg(token=None)])
        ws.close.side_effect = OSError("connection already closed")
        listener._handle_connection(ws)  # must not raise


# ---------------------------------------------------------------------------
# stop() — server shutdown
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_shuts_down_server_and_clears_reference(self):
        """stop() calls shutdown() on the server and sets _server to None."""
        from unittest.mock import MagicMock

        listener, _ = _build_listener()
        mock_server = MagicMock()
        listener._server = mock_server

        listener.stop()

        mock_server.shutdown.assert_called_once()
        assert listener._server is None

    def test_stop_noop_when_no_server(self):
        """stop() is a no-op when _server is None."""
        listener, _ = _build_listener()
        listener._server = None
        listener.stop()  # must not raise


# ---------------------------------------------------------------------------
# Token file read error (OSError on disk)
# ---------------------------------------------------------------------------


class TestTokenFileReadError:
    def test_oserror_reading_token_file_falls_through_to_auth_fail(self, tmp_path):
        """If reading the stored token raises OSError, the provided token cannot be
        validated and auth_fail is sent."""
        from unittest.mock import patch

        remote_id = "test-remote-uuid"
        remotes_path = tmp_path / "remotes"
        token_dir = remotes_path / remote_id
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "token").write_text("correct-token")

        listener, on_conn = _build_listener(remotes_path=remotes_path)
        ws = _make_ws([_auth_msg(token="correct-token", remote_id=remote_id)])

        with patch("pathlib.Path.read_text", side_effect=OSError("disk error")):
            with pytest.raises(ValueError, match="Invalid token"):
                listener._authenticate(ws, "127.0.0.1:1234")

        text_sends = [c.args[0] for c in ws.send.call_args_list if isinstance(c.args[0], str)]
        assert any('"auth_fail"' in m for m in text_sends)
        on_conn.assert_not_called()


# ---------------------------------------------------------------------------
# Token directory creation error (OSError during adoption)
# ---------------------------------------------------------------------------


class TestTokenDirectoryCreateError:
    def test_oserror_creating_token_dir_raises_runtime_error(self, tmp_path):
        """If mkdir raises OSError during adoption, a RuntimeError is raised."""
        from unittest.mock import patch

        remotes_path = tmp_path / "remotes"
        listener, on_conn = _build_listener(
            remotes_path=remotes_path,
            get_pairing_active=lambda: True,
        )
        ws = _make_ws([_auth_msg(token=None)])

        with patch("pathlib.Path.mkdir", side_effect=OSError("read-only filesystem")):
            with pytest.raises(RuntimeError, match="Could not save token"):
                listener._authenticate(ws, "127.0.0.1:1234")

        on_conn.assert_not_called()


# ---------------------------------------------------------------------------
# auth_ack edge cases (adoption path)
# ---------------------------------------------------------------------------


class TestAuthAckEdgeCases:
    def test_malformed_json_ack_raises(self):
        """A non-JSON auth_ack text message raises ValueError."""
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp())
        remotes_path = tmp / "remotes"
        listener, on_conn = _build_listener(
            remotes_path=remotes_path,
            get_pairing_active=lambda: True,
        )
        ws = _make_ws(
            [
                _auth_msg(token=None),
                "not valid json }{{",  # malformed ack
            ]
        )
        # Malformed JSON → ack_msg becomes {} → type != "auth_ack" → ValueError
        with pytest.raises(ValueError, match="auth_ack"):
            listener._authenticate(ws, "127.0.0.1:1234")

    def test_wrong_ack_type_raises(self):
        """An auth_ack message with the wrong type field raises ValueError."""
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp())
        remotes_path = tmp / "remotes"
        listener, on_conn = _build_listener(
            remotes_path=remotes_path,
            get_pairing_active=lambda: True,
        )
        ws = _make_ws(
            [
                _auth_msg(token=None),
                json.dumps({"type": "unexpected"}),
            ]
        )
        with pytest.raises(ValueError, match="auth_ack"):
            listener._authenticate(ws, "127.0.0.1:1234")

    def test_binary_ack_raises(self):
        """A binary (non-text) auth_ack message raises ValueError."""
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp())
        remotes_path = tmp / "remotes"
        listener, on_conn = _build_listener(
            remotes_path=remotes_path,
            get_pairing_active=lambda: True,
        )
        ws = _make_ws(
            [
                _auth_msg(token=None),
                b"\xaa\x55\x00\x00",  # binary, not text
            ]
        )
        with pytest.raises(ValueError, match="auth_ack text message"):
            listener._authenticate(ws, "127.0.0.1:1234")


# ---------------------------------------------------------------------------
# replay_done edge cases
# ---------------------------------------------------------------------------


class TestReplayDoneEdgeCases:
    def _make_frame(self) -> bytes:
        return b"\x01" * 64

    def test_malformed_json_replay_done_is_warned_not_raised(self):
        """A non-JSON replay_done text message logs a warning but does not raise."""
        token = "my-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)
        ws = _make_ws(
            [
                _auth_msg(token=token, queue_depth=1),
                self._make_frame(),
                "not valid json }{{",  # malformed replay_done
            ]
        )
        # Must not raise — just warns
        listener._authenticate(ws, "127.0.0.1:1234")
        on_conn.assert_called_once()

    def test_wrong_replay_done_type_is_warned_not_raised(self):
        """A replay_done message with the wrong type field logs a warning but proceeds."""
        token = "my-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)
        ws = _make_ws(
            [
                _auth_msg(token=token, queue_depth=1),
                self._make_frame(),
                json.dumps({"type": "unexpected"}),
            ]
        )
        listener._authenticate(ws, "127.0.0.1:1234")
        on_conn.assert_called_once()

    def test_binary_replay_done_is_warned_not_raised(self):
        """A binary replay_done message logs a warning but does not raise."""
        token = "my-token"
        listener, on_conn, _ = _build_listener_with_stored_token(token)
        ws = _make_ws(
            [
                _auth_msg(token=token, queue_depth=1),
                self._make_frame(),
                b"\xaa\x55\x00\x00",  # binary instead of text
            ]
        )
        listener._authenticate(ws, "127.0.0.1:1234")
        on_conn.assert_called_once()
