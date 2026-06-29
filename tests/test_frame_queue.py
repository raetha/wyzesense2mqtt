"""
Tests for remote/frame_queue.InMemoryFrameQueue.

No hardware, no network — pure in-memory ring buffer logic.
"""

import os
import sys

# Make the remote/ package importable from the tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remote"))

from frame_queue import InMemoryFrameQueue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _frame(b: int, size: int = 64) -> bytes:
    return bytes([b]) * size


# ---------------------------------------------------------------------------
# Basic push / depth
# ---------------------------------------------------------------------------


class TestPushAndDepth:
    def test_empty_on_creation(self):
        q = InMemoryFrameQueue()
        assert q.depth == 0

    def test_depth_increments(self):
        q = InMemoryFrameQueue()
        q.push(_frame(1), "event")
        q.push(_frame(2), "handshake")
        assert q.depth == 2

    def test_clear_resets_depth(self):
        q = InMemoryFrameQueue()
        q.push(_frame(1), "event")
        q.clear()
        assert q.depth == 0

    def test_clear_empties_replay(self):
        q = InMemoryFrameQueue()
        q.push(_frame(1), "handshake")
        q.clear()
        assert q.get_replay_frames() == []


# ---------------------------------------------------------------------------
# get_replay_frames — handshake always returned
# ---------------------------------------------------------------------------


class TestHandshakeRetained:
    def test_handshake_always_included(self):
        q = InMemoryFrameQueue(max_seconds=0.0)  # zero TTL for events
        f = _frame(0xAA)
        q.push(f, "handshake")
        frames = q.get_replay_frames()
        assert f in frames

    def test_multiple_handshake_frames_all_returned(self):
        q = InMemoryFrameQueue(max_seconds=0.0)
        frames = [_frame(i) for i in range(5)]
        for f in frames:
            q.push(f, "handshake")
        result = q.get_replay_frames()
        for f in frames:
            assert f in result

    def test_order_preserved(self):
        q = InMemoryFrameQueue()
        frames = [_frame(i) for i in range(4)]
        for f in frames:
            q.push(f, "handshake")
        assert q.get_replay_frames() == frames


# ---------------------------------------------------------------------------
# get_replay_frames — event frame TTL
# ---------------------------------------------------------------------------


class TestEventTTL:
    def test_recent_event_included(self):
        q = InMemoryFrameQueue(max_seconds=10.0)
        f = _frame(0x01)
        q.push(f, "event")
        assert f in q.get_replay_frames()

    def test_stale_event_excluded(self, monkeypatch):
        """An event pushed long ago should be excluded when its TTL has expired."""
        import frame_queue as fq_module

        q = InMemoryFrameQueue(max_seconds=5.0)
        # Push the event at t=0
        start = 1000.0
        monkeypatch.setattr(fq_module.time, "monotonic", lambda: start)
        f = _frame(0xFF)
        q.push(f, "event")

        # Advance clock past the TTL
        monkeypatch.setattr(fq_module.time, "monotonic", lambda: start + 10.0)
        assert f not in q.get_replay_frames()

    def test_event_within_ttl_included(self, monkeypatch):
        import frame_queue as fq_module

        q = InMemoryFrameQueue(max_seconds=5.0)
        start = 1000.0
        monkeypatch.setattr(fq_module.time, "monotonic", lambda: start)
        f = _frame(0x0F)
        q.push(f, "event")

        # Still within TTL
        monkeypatch.setattr(fq_module.time, "monotonic", lambda: start + 3.0)
        assert f in q.get_replay_frames()

    def test_handshake_included_even_when_event_is_stale(self, monkeypatch):
        import frame_queue as fq_module

        q = InMemoryFrameQueue(max_seconds=1.0)
        start = 1000.0
        monkeypatch.setattr(fq_module.time, "monotonic", lambda: start)

        hs_frame = _frame(0x01)
        ev_frame = _frame(0x02)
        q.push(hs_frame, "handshake")
        q.push(ev_frame, "event")

        # Advance past event TTL
        monkeypatch.setattr(fq_module.time, "monotonic", lambda: start + 5.0)
        result = q.get_replay_frames()
        assert hs_frame in result
        assert ev_frame not in result


# ---------------------------------------------------------------------------
# Eviction policy (max_frames)
# ---------------------------------------------------------------------------


class TestEviction:
    def test_oldest_event_evicted_when_over_limit(self):
        q = InMemoryFrameQueue(max_frames=3)
        f1 = _frame(0x01)
        f2 = _frame(0x02)
        f3 = _frame(0x03)
        f4 = _frame(0x04)  # causes eviction
        q.push(f1, "event")
        q.push(f2, "event")
        q.push(f3, "event")
        q.push(f4, "event")
        assert q.depth == 3
        result = q.get_replay_frames()
        assert f1 not in result  # oldest evicted
        assert f4 in result  # newest retained

    def test_handshake_protected_from_eviction_when_events_exist(self):
        q = InMemoryFrameQueue(max_frames=3)
        hs = _frame(0xAA)
        q.push(hs, "handshake")
        q.push(_frame(0x01), "event")
        q.push(_frame(0x02), "event")
        # Adding a 4th frame: oldest event should be evicted, not the handshake
        q.push(_frame(0x03), "event")
        assert q.depth == 3
        result = q.get_replay_frames()
        assert hs in result
        assert _frame(0x01) not in result

    def test_handshake_evicted_as_last_resort(self):
        """When buffer is full of handshake frames and a new one arrives, oldest is dropped."""
        q = InMemoryFrameQueue(max_frames=2)
        h1 = _frame(0x01)
        h2 = _frame(0x02)
        h3 = _frame(0x03)
        q.push(h1, "handshake")
        q.push(h2, "handshake")
        q.push(h3, "handshake")  # forces eviction of h1 (no events to evict)
        assert q.depth == 2
        result = q.get_replay_frames()
        assert h1 not in result
        assert h3 in result

    def test_depth_never_exceeds_max_frames(self):
        q = InMemoryFrameQueue(max_frames=10)
        for i in range(25):
            q.push(_frame(i & 0xFF), "event")
        assert q.depth == 10
