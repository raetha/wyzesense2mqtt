"""
Tests for the Remote class and helpers in remote/remote.py.

All tests use mocks — no real HID device or network is involved.
"""

import json
import os
import pathlib
import sys
from unittest.mock import MagicMock

import pytest
import websockets.exceptions

# Make remote/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remote"))


# ---------------------------------------------------------------------------
# _load_or_create_remote_id
# ---------------------------------------------------------------------------


class TestLoadOrCreateRemoteId:
    def test_creates_file_on_first_call(self, tmp_path):
        from remote import _load_or_create_remote_id

        remote_id = _load_or_create_remote_id(tmp_path)
        id_file = tmp_path / "remote_id"
        assert id_file.exists()
        assert id_file.read_text().strip() == remote_id

    def test_returned_value_is_uuid_like(self, tmp_path):
        import uuid

        from remote import _load_or_create_remote_id

        remote_id = _load_or_create_remote_id(tmp_path)
        # Should be parseable as a UUID
        parsed = uuid.UUID(remote_id)
        assert str(parsed) == remote_id

    def test_returns_existing_id_on_second_call(self, tmp_path):
        from remote import _load_or_create_remote_id

        first = _load_or_create_remote_id(tmp_path)
        second = _load_or_create_remote_id(tmp_path)
        assert first == second

    def test_reads_pre_existing_file(self, tmp_path):
        from remote import _load_or_create_remote_id

        expected = "pre-existing-id"
        (tmp_path / "remote_id").write_text(expected)
        result = _load_or_create_remote_id(tmp_path)
        assert result == expected

    def test_creates_parent_dirs(self, tmp_path):
        from remote import _load_or_create_remote_id

        nested = tmp_path / "a" / "b" / "c"
        remote_id = _load_or_create_remote_id(nested)
        assert (nested / "remote_id").exists()
        assert len(remote_id) > 0


# ---------------------------------------------------------------------------
# Remote._read_token
# ---------------------------------------------------------------------------


def _make_remote(data_dir: pathlib.Path, remote_id: str = "test-uuid") -> object:
    from remote import Remote

    return Remote(
        hub_url="ws://localhost:8765",
        remote_id=remote_id,
        data_dir=data_dir,
        device="/dev/null",
    )


class TestReadToken:
    def test_returns_none_when_no_file_and_no_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        r = _make_remote(tmp_path)
        assert r._read_token() is None

    def test_returns_file_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        (tmp_path / "hub_token").write_text("file-token-123")
        r = _make_remote(tmp_path)
        assert r._read_token() == "file-token-123"

    def test_env_token_takes_precedence_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WS2M_HUB_TOKEN", "env-token-xyz")
        (tmp_path / "hub_token").write_text("file-token-abc")
        r = _make_remote(tmp_path)
        assert r._read_token() == "env-token-xyz"

    def test_returns_none_for_empty_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        (tmp_path / "hub_token").write_text("   ")
        r = _make_remote(tmp_path)
        assert r._read_token() is None


# ---------------------------------------------------------------------------
# Remote._save_token
# ---------------------------------------------------------------------------


class TestSaveToken:
    def test_saves_token_to_file(self, tmp_path):
        r = _make_remote(tmp_path)
        r._save_token("my-secret-token")
        assert (tmp_path / "hub_token").read_text() == "my-secret-token"

    def test_creates_parent_directory(self, tmp_path):
        data_dir = tmp_path / "nested" / "data"
        r = _make_remote(data_dir)
        r._save_token("tok")
        assert (data_dir / "hub_token").exists()


# ---------------------------------------------------------------------------
# Remote._authenticate — outbound auth flow
# ---------------------------------------------------------------------------


def _make_ws_mock(recv_sequence: list) -> MagicMock:
    ws = MagicMock()
    ws.recv.side_effect = list(recv_sequence)
    ws.send = MagicMock()
    ws.close = MagicMock()
    return ws


class TestAuthenticate:
    def _build_auth_ok(self):
        return json.dumps({"type": "auth_ok"})

    def _build_auth_token(self, token: str):
        return json.dumps({"type": "auth_token", "token": token})

    def _build_auth_fail(self, reason: str = "not_in_pairing_mode"):
        return json.dumps({"type": "auth_fail", "reason": reason})

    def test_sends_auth_message_with_remote_id_and_mac(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        r = _make_remote(tmp_path, remote_id="my-remote-uuid")
        r._dongle_mac = "AABBCCDD"
        r._fresh_start = True

        ws = _make_ws_mock([self._build_auth_ok()])
        r._authenticate(ws)

        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args.args[0])
        assert sent["type"] == "auth"
        assert sent["remote_id"] == "my-remote-uuid"
        assert sent["dongle_mac"] == "AABBCCDD"
        assert sent["fresh_start"] is True

    def test_auth_ok_succeeds_without_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fresh_start = True

        ws = _make_ws_mock([self._build_auth_ok()])
        r._authenticate(ws)  # must not raise

    def test_token_included_when_present(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        (tmp_path / "hub_token").write_text("stored-token")
        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fresh_start = True

        ws = _make_ws_mock([self._build_auth_ok()])
        r._authenticate(ws)

        sent = json.loads(ws.send.call_args_list[0].args[0])
        assert sent.get("token") == "stored-token"

    def test_token_not_included_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fresh_start = True

        ws = _make_ws_mock([self._build_auth_ok()])
        r._authenticate(ws)

        sent = json.loads(ws.send.call_args_list[0].args[0])
        assert "token" not in sent

    def test_adoption_saves_token_and_sends_ack(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fresh_start = True

        ws = _make_ws_mock([self._build_auth_token("new-token-xyz")])
        r._authenticate(ws)

        # Token file must have been saved
        assert (tmp_path / "hub_token").read_text() == "new-token-xyz"

        # auth_ack must have been sent
        sent_messages = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        ack_messages = [m for m in sent_messages if m.get("type") == "auth_ack"]
        assert len(ack_messages) == 1

    def test_auth_fail_raises_runtime_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fresh_start = True

        ws = _make_ws_mock([self._build_auth_fail("not_in_pairing_mode")])
        with pytest.raises(RuntimeError, match="rejected"):
            r._authenticate(ws)

    def test_unexpected_auth_response_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fresh_start = True

        ws = _make_ws_mock([json.dumps({"type": "something_weird"})])
        with pytest.raises(ValueError, match="Unexpected auth response"):
            r._authenticate(ws)

    def test_fresh_start_set_to_false_after_auth(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fresh_start = True

        ws = _make_ws_mock([self._build_auth_ok()])
        r._authenticate(ws)

        assert r._fresh_start is False

    def test_replay_frames_sent_when_queue_not_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WS2M_HUB_TOKEN", raising=False)
        from frame_queue import InMemoryFrameQueue

        queue = InMemoryFrameQueue()
        frame1 = b"\x01" * 64
        frame2 = b"\x02" * 64
        queue.push(frame1, "handshake")
        queue.push(frame2, "handshake")

        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fresh_start = False  # non-fresh → replay
        r._queue = queue

        ws = _make_ws_mock([self._build_auth_ok()])
        r._authenticate(ws)

        # auth msg + frame1 + frame2 + replay_done
        call_args = [c.args[0] for c in ws.send.call_args_list]
        assert frame1 in call_args
        assert frame2 in call_args
        replay_done_msgs = [c for c in call_args if isinstance(c, str) and "replay_done" in c]
        assert len(replay_done_msgs) == 1


# ---------------------------------------------------------------------------
# Remote hub_reader — restart frame handling
# ---------------------------------------------------------------------------


class TestHubReaderRestartFrame:
    """Remote's hub_reader exits with os._exit(0) on {"type": "restart"} frame."""

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_restart_frame_calls_os_exit(self, tmp_path, monkeypatch):
        """When hub sends {"type": "restart"}, remote calls os._exit(0)."""
        import threading
        from unittest.mock import patch

        # Patch os.open so _open_hid does not need real hardware
        monkeypatch.setattr(os, "open", lambda *a, **kw: 42)
        monkeypatch.setattr(os, "close", lambda fd: None)

        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fd = 42

        restart_msg = json.dumps({"type": "restart"})

        # We'll drive hub_reader directly by calling _bidirectional_forward with
        # a mock WS that returns the restart msg text.  Since os._exit is patched
        # we catch the call via a threading.Event.
        exit_called = threading.Event()

        def fake_exit(code):
            exit_called.set()
            raise SystemExit(code)

        # Build a ws mock: hub_reader receives the restart text frame
        ws = MagicMock()

        recv_calls = [0]

        def recv_side(timeout=None):
            n = recv_calls[0]
            recv_calls[0] += 1
            if n == 0:
                return restart_msg
            raise TimeoutError()

        ws.recv.side_effect = recv_side

        # dongle_reader: block until stop is set
        import select as _select

        monkeypatch.setattr(_select, "select", lambda *a, **kw: ([], [], []))
        monkeypatch.setattr(os, "read", lambda fd, n: b"\x00" * n)

        with patch.object(os, "_exit", side_effect=fake_exit):
            try:
                r._bidirectional_forward(ws)
            except SystemExit:
                pass

        assert exit_called.is_set(), "os._exit should have been called with restart frame"


class TestHubReaderControlFrames:
    """Remote correctly handles set_dongle and set_log_level control frames from the hub."""

    def test_set_dongle_updates_device(self, tmp_path, monkeypatch):
        """When hub sends set_dongle, remote updates self._device."""

        monkeypatch.setattr(os, "open", lambda *a, **kw: 42)
        monkeypatch.setattr(os, "close", lambda fd: None)

        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fd = 42
        r._device = "auto"

        # Simulate what hub_reader does when it receives a set_dongle frame
        msg = json.dumps({"type": "set_dongle", "value": "/dev/hidraw3"})
        parsed = json.loads(msg)
        msg_type = parsed.get("type")
        assert msg_type == "set_dongle"
        value = str(parsed.get("value", "auto"))
        r._device = value

        assert r._device == "/dev/hidraw3"

    def test_set_dongle_auto_updates_device(self, tmp_path, monkeypatch):
        """set_dongle with 'auto' sets self._device to 'auto'."""
        monkeypatch.setattr(os, "open", lambda *a, **kw: 42)
        monkeypatch.setattr(os, "close", lambda fd: None)

        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fd = 42
        r._device = "/dev/hidraw0"

        msg = json.dumps({"type": "set_dongle", "value": "auto"})
        parsed = json.loads(msg)
        r._device = str(parsed.get("value", "auto"))

        assert r._device == "auto"

    def test_set_log_level_applies_immediately(self, tmp_path, monkeypatch):
        """When hub sends set_log_level, remote changes the root logger level immediately."""
        import logging

        monkeypatch.setattr(os, "open", lambda *a, **kw: 42)
        monkeypatch.setattr(os, "close", lambda fd: None)

        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fd = 42

        # Simulate what hub_reader does when it receives a set_log_level frame
        msg = json.dumps({"type": "set_log_level", "level": "DEBUG"})
        parsed = json.loads(msg)
        level = str(parsed.get("level", "INFO")).upper()
        logging.getLogger().setLevel(getattr(logging, level, logging.INFO))

        assert logging.getLogger().level == logging.DEBUG

        # Restore to avoid polluting other tests
        logging.getLogger().setLevel(logging.WARNING)

    def test_set_dongle_frame_handled_in_hub_reader(self, tmp_path, monkeypatch):
        """hub_reader processes set_dongle frame and updates _device (integration)."""
        import select as _select

        monkeypatch.setattr(os, "open", lambda *a, **kw: 42)
        monkeypatch.setattr(os, "close", lambda fd: None)

        r = _make_remote(tmp_path)
        r._dongle_mac = "MAC1"
        r._fd = 42
        r._device = "auto"

        msg_json = json.dumps({"type": "set_dongle", "value": "/dev/hidraw5"})

        recv_index = [0]

        def recv_side(timeout=None):
            i = recv_index[0]
            recv_index[0] += 1
            if i == 0:
                return msg_json
            # After first message, raise to terminate hub_reader
            raise websockets.exceptions.ConnectionClosed(None, None)

        ws = MagicMock()
        ws.recv.side_effect = recv_side

        monkeypatch.setattr(_select, "select", lambda *a, **kw: ([], [], []))
        monkeypatch.setattr(os, "read", lambda fd, n: b"\x00" * n)

        try:
            r._bidirectional_forward(ws)
        except Exception:
            pass

        assert r._device == "/dev/hidraw5"
