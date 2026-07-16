"""
Frame ring buffer for the ws2m relay process.

Stores raw HID frames captured from the dongle for replay on hub reconnect.

Frame types
-----------
  handshake  The first N frames received after a fresh dongle-connect.  Covers
             the hub's 5-step init sequence (inquiry → get_enr → get_mac →
             get_version → finish_auth) and is always retained in the buffer.
  event      All subsequent frames (sensor alarms, heartbeats, etc.).  Retained
             only while within queue_max_seconds of the current time.

Eviction policy
---------------
When the buffer reaches queue_max_frames, the oldest *event* frame is evicted
first.  Handshake frames are only evicted as a last resort (when the buffer
consists entirely of handshake frames).

Thread safety
-------------
push() is called from the dongle-reader thread.
get_replay_frames() and clear() are called from the connection thread.
An internal lock serialises all mutations.
"""

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Literal

FrameType = Literal["handshake", "event"]


class FrameQueue(ABC):
    """Abstract ring buffer for raw HID frames queued for hub replay."""

    @abstractmethod
    def push(self, frame: bytes, frame_type: FrameType) -> None:
        """Add *frame* to the buffer, evicting older frames as needed."""

    @abstractmethod
    def get_replay_frames(self) -> list[bytes]:
        """Return the frames to send on reconnect.

        Always includes all handshake frames.
        Includes event frames whose timestamp is within queue_max_seconds.
        """

    @abstractmethod
    def clear(self) -> None:
        """Discard all buffered frames."""

    @property
    @abstractmethod
    def depth(self) -> int:
        """Current number of frames in the buffer."""


class InMemoryFrameQueue(FrameQueue):
    """In-memory ring buffer backed by a :class:`collections.deque`.

    Parameters
    ----------
    max_seconds:
        TTL for *event* frames in seconds (default 10).
    max_frames:
        Maximum total frames in the buffer (default 500).
    """

    def __init__(self, max_seconds: float = 10.0, max_frames: int = 500):
        self._max_seconds = max_seconds
        self._max_frames = max_frames
        # Each element: (timestamp: float, frame_type: FrameType, frame: bytes)
        self._queue: deque[tuple[float, FrameType, bytes]] = deque()
        self._lock = threading.Lock()

    def push(self, frame: bytes, frame_type: FrameType) -> None:
        with self._lock:
            self._queue.append((time.monotonic(), frame_type, frame))
            # Trim to max_frames: evict oldest event frame first
            while len(self._queue) > self._max_frames:
                for i, (_, ft, _) in enumerate(self._queue):
                    if ft == "event":
                        del self._queue[i]
                        break
                else:
                    # No event frames to evict — drop oldest overall
                    self._queue.popleft()

    def get_replay_frames(self) -> list[bytes]:
        now = time.monotonic()
        cutoff = now - self._max_seconds
        with self._lock:
            return [frame for ts, ft, frame in self._queue if ft == "handshake" or ts >= cutoff]

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._queue)
