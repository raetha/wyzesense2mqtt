"""
Tests for the Transport abstraction in dongle_protocol.py.

LocalTransport and RemoteTransport are exercised without real USB hardware
using mocks and in-memory stubs.
"""

import json
import os
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# LocalTransport
# ---------------------------------------------------------------------------


class TestLocalTransport:
    """LocalTransport wraps an OS file descriptor obtained via os.open."""

    def test_open_calls_os_open(self, monkeypatch):
        from dongle_protocol import LocalTransport

        with patch("dongle_protocol.os.open", return_value=7) as mock_open:
            t = LocalTransport("/dev/hidraw0")
            mock_open.assert_called_once_with("/dev/hidraw0", os.O_RDWR | os.O_NONBLOCK)
            assert t._fd == 7

    def test_read_delegates_to_os_read(self, monkeypatch):
        from dongle_protocol import LocalTransport

        payload = b"\xaa" * 64
        with patch("dongle_protocol.os.open", return_value=5):
            t = LocalTransport("/dev/hidraw0")
        with patch("dongle_protocol.os.read", return_value=payload) as mock_read:
            result = t.read()
        mock_read.assert_called_once_with(5, 0x40)
        assert result == payload

    def test_write_delegates_to_os_write(self, monkeypatch):
        from dongle_protocol import LocalTransport

        data = b"\xaa\x55\x43\x03\x04\x01\x49"
        with patch("dongle_protocol.os.open", return_value=5):
            t = LocalTransport("/dev/hidraw0")
        with patch("dongle_protocol.os.write", return_value=len(data)) as mock_write:
            t.write(data)
        mock_write.assert_called_once_with(5, data)

    def test_close_calls_os_close_once(self):
        from dongle_protocol import LocalTransport

        with patch("dongle_protocol.os.open", return_value=5):
            t = LocalTransport("/dev/hidraw0")
        with patch("dongle_protocol.os.close") as mock_close:
            t.close()
            t.close()  # second call is a no-op
        mock_close.assert_called_once_with(5)

    def test_remote_id_is_none(self):
        from dongle_protocol import LocalTransport

        with patch("dongle_protocol.os.open", return_value=5):
            t = LocalTransport("/dev/hidraw0")
        # LocalTransport has no remote_id attribute — that lives on RemoteTransport
        assert not hasattr(t, "remote_id")


# ---------------------------------------------------------------------------
# RemoteTransport
# ---------------------------------------------------------------------------


def _make_ws_mock(recv_frames: list) -> MagicMock:
    """Build a mock WebSocket that yields frames from recv_frames in order."""
    ws = MagicMock()
    ws.recv.side_effect = recv_frames
    ws.send = MagicMock()
    ws.close = MagicMock()
    return ws


class TestRemoteTransport:
    """RemoteTransport drains the replay deque before switching to live recv()."""

    def test_remote_id_property(self):
        from dongle_protocol import RemoteTransport

        ws = _make_ws_mock([])
        t = RemoteTransport(ws, remote_id="pi-floor2", dongle_mac="AABBCCDD", replay_frames=[])
        assert t.remote_id == "pi-floor2"

    def test_dongle_mac_property(self):
        from dongle_protocol import RemoteTransport

        ws = _make_ws_mock([])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="AABBCCDD", replay_frames=[])
        assert t.dongle_mac == "AABBCCDD"

    def test_replay_frames_drained_before_live(self):
        from dongle_protocol import RemoteTransport

        replay = [b"\x01" * 64, b"\x02" * 64]
        live = b"\x03" * 64
        ws = _make_ws_mock([live])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=list(replay))

        assert t.read() == replay[0]
        assert t.read() == replay[1]
        assert t.read() == live  # now switches to ws.recv()
        ws.recv.assert_called_once()

    def test_empty_replay_goes_straight_to_ws(self):
        from dongle_protocol import RemoteTransport

        live = b"\xaa" * 64
        ws = _make_ws_mock([live])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        result = t.read()
        assert result == live
        ws.recv.assert_called_once()

    def test_write_calls_ws_send(self):
        from dongle_protocol import RemoteTransport

        ws = _make_ws_mock([])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        data = b"\xaa\x55\x43\x03\x04\x01\x49"
        t.write(data)
        ws.send.assert_called_once_with(data)

    def test_close_calls_ws_close(self):
        from dongle_protocol import RemoteTransport

        ws = _make_ws_mock([])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        t.close()
        ws.close.assert_called_once()

    def test_close_swallows_exceptions(self):
        from dongle_protocol import RemoteTransport

        ws = MagicMock()
        ws.close.side_effect = RuntimeError("already closed")
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        t.close()  # must not raise

    def test_text_messages_from_ws_skipped_and_returns_empty(self):
        """Unexpected JSON control messages during forwarding are skipped."""
        from dongle_protocol import RemoteTransport

        text_msg = '{"type": "ping"}'
        binary_msg = b"\x05" * 64
        ws = _make_ws_mock([text_msg, binary_msg])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])

        result1 = t.read()  # receives text → returns b""
        assert result1 == b""
        result2 = t.read()  # receives binary → returns it
        assert result2 == binary_msg

    def test_remote_unhealthy_triggers_callback_and_returns_empty(self):
        """remote_unhealthy JSON text frame triggers on_health_change(remote_id, False)."""
        from dongle_protocol import RemoteTransport

        health_calls = []

        def callback(rid, healthy):
            health_calls.append((rid, healthy))

        unhealthy_msg = json.dumps({"type": "remote_unhealthy", "reason": "dongle_lost"})
        binary_msg = b"\xaa" * 64
        ws = _make_ws_mock([unhealthy_msg, binary_msg])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[], on_health_change=callback)

        result1 = t.read()  # health message → b""
        assert result1 == b""
        assert health_calls == [("r1", False)]

        result2 = t.read()  # binary frame passes through
        assert result2 == binary_msg

    def test_remote_healthy_triggers_callback_and_returns_empty(self):
        """remote_healthy JSON text frame triggers on_health_change(remote_id, True)."""
        from dongle_protocol import RemoteTransport

        health_calls = []

        def callback(rid, healthy):
            health_calls.append((rid, healthy))

        healthy_msg = json.dumps({"type": "remote_healthy"})
        ws = _make_ws_mock([healthy_msg])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[], on_health_change=callback)

        result = t.read()
        assert result == b""
        assert health_calls == [("r1", True)]

    def test_health_message_without_callback_does_not_raise(self):
        """Health messages with no callback set are silently ignored."""
        import json

        from dongle_protocol import RemoteTransport

        unhealthy_msg = json.dumps({"type": "remote_unhealthy", "reason": "dongle_lost"})
        ws = _make_ws_mock([unhealthy_msg])
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        result = t.read()  # must not raise
        assert result == b""

    def test_send_restart_sends_correct_json_frame(self):
        """send_restart() sends {"type": "restart"} as a JSON text frame."""
        import json

        from dongle_protocol import RemoteTransport

        ws = _make_ws_mock([])
        ws.send = MagicMock()
        t = RemoteTransport(ws, remote_id="r1", dongle_mac="MAC1", replay_frames=[])
        t.send_restart()

        ws.send.assert_called_once()
        sent = ws.send.call_args.args[0]
        assert isinstance(sent, str)
        assert json.loads(sent) == {"type": "restart"}
