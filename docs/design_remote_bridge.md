# Design: Remote Bridge (Python / Docker)

This document covers the Python-based remote bridge implementation. For the ESP32/ESPHome variant see [`docs/design_remote_bridge_esphome.md`](design_remote_bridge_esphome.md).

---

## Terminology

| Term | Meaning |
|---|---|
| **Hub** | The central ws2m process (currently in `wyzesense2mqtt/`). Connects to MQTT, manages HA discovery, owns the sensor registry. |
| **Remote** | A lightweight relay process running on a separate machine. Holds a USB dongle, forwards HID frames to the hub over WebSocket. |
| **Dongle** | The WyzeSense USB HID bridge device (vendor `1a86`, product `e024`). |

---

## Repository Structure (Proposed Rename)

As part of implementing the remote bridge, the repo layout should be restructured to reflect the hub/remote split. This is a rename of the Python package directory — no functional changes to the hub code.

```
wyzesense2mqtt/           ← current
hub/                      ← proposed rename
  __main__.py
  bridge.py
  config.py
  dongle_protocol.py
  mqtt.py
  sensors.py
  requirements.txt
  Dockerfile              ← moved from project root

remote/                   ← new
  __main__.py
  relay.py
  queue.py
  requirements.txt
  Dockerfile

cli/                      ← stays at root (shared tooling)
  dongle_tool.py
  mqtt_tool.py

docs/
examples/
  hub/
    docker-compose.yml.example
    .env.example
  remote/
    docker-compose.yml.example
    .env.example
scripts/
tests/
.github/
  workflows/
    ci.yml                ← ruff + tests (hub only)
    hub_release.yml       ← publish ghcr.io/raetha/ws2m-hub
    remote_release.yml    ← publish ghcr.io/raetha/ws2m-remote
    devel_package.yml     ← :devel image (hub)
```

The Python package import name for the hub remains `wyzesense2mqtt` (or becomes `hub`) — this is an internal detail that doesn't affect Docker users. The CLI tools in `cli/` are accessible from both hub and remote images where relevant; `dongle_tool` is included in the remote image since it operates the local dongle.

---

## Architecture Decision: WebSocket Passthrough with Replay Buffer

Three transport options were considered and WebSocket passthrough was selected (see original rationale). This section adds the queuing design.

### The raw frame replay approach

Your question about whether the queue can include initialization sequence frames is the right one, and the answer is yes — and it actually makes queuing *simpler* than the parsed-event alternative.

The relay queues the complete ordered stream of raw HID bytes from the dongle, including handshake frames, with timestamps. On reconnect, it sends everything in the buffer before resuming the live stream. The hub's protocol state machine processes the replayed frames exactly as if it had been connected the whole time: handshake frames first, then event frames. The relay stays fully transparent — it never needs to understand the protocol.

This works because:
1. The HID handshake is deterministic and short (~5 round trips, ~100 ms)
2. The hub already handles "cold start" after a disconnect by re-running the handshake
3. Raw frames have precise timestamps attached by the relay, so the hub knows the original event times

The only constraint is that the replay window must be short enough that the hub's protocol state doesn't diverge significantly. A window of a few seconds is appropriate; minutes or hours is not.

---

## Queue Design: Short Ring Buffer with TTL

### Philosophy

The goal is robustness against brief network disruptions (milliseconds to a few seconds) on wireless/mesh networks — not long-term event preservation. A mesh network dropping one AP for a few seconds, or a brief Wi-Fi association event, should not lose a door-open event that would trigger an automation. A 30-minute power outage almost certainly will lose data, and that's acceptable because the automation context has changed anyway.

### Ring buffer behaviour

The relay maintains a queue of `(timestamp, frame_type, raw_frame_bytes)` tuples. Frame type is either `handshake` or `event` — determined by counting: the relay marks the first fixed number of frames after dongle connect as handshake frames (the 5-step init sequence is a predictable length). This requires no protocol parsing.

The queue has two limits:

- **`queue_max_seconds`** (default: `10`): the TTL for *event* frames. Handshake frames are always retained regardless of age.
- **`queue_max_frames`** (default: `500`): maximum buffer size. When full, oldest event frames are dropped first; handshake frames are never evicted by this limit.

On network reconnect:

1. **Always replay all handshake frames** — they are always valid to send. The dongle accepts re-inquiry at any time, and the hub expects to run the handshake on every fresh connection. Sending only handshake frames with nothing following is also fine — that is exactly what a normal cold start looks like; the hub runs the handshake and then waits for live events.
2. **Replay event frames that are within TTL** — trim event frames older than `queue_max_seconds` from the buffer before replaying. Recent events (door open 2 seconds ago) are replayed; stale heartbeats from before the outage are dropped.
3. **Send `replay_done`** — hub resumes normal live operation.

This means we never need to drop recent frames regardless of outage length. The handshake is always intact. The only frames that are ever dropped are non-handshake frames outside the TTL window — heartbeats and sensor events from before the outage that are no longer actionable.

The relay signals replay intent in the auth handshake (see below).

### Why not SQLite for a short window?

For a 10-second window, SQLite adds complexity without benefit — the volume of data is tiny and in-memory is reliable enough since a relay restart that clears the buffer is itself a signal to the hub to start fresh. SQLite becomes worthwhile if the window is extended to minutes. The queue implementation should be abstracted behind an interface so SQLite can be swapped in later without changing the relay logic.

### Relay restart handling

If the relay restarts (power cycle, crash), its buffer is empty. It includes a `fresh_start: true` flag in the auth handshake and `queue_depth: 0`. The hub treats this as a normal cold start — fresh handshake, no replay.

---

## Connection Sequencing and Race Conditions

### The race condition

Either side can restart independently:

- **Remote restarts while hub is running**: remote comes up, completes local HID handshake, connects to hub, sends auth with `dongle_mac`. Hub re-registers the dongle idempotently (same MAC = same entry, no duplicate). Hub re-runs its own init sequence with the remote's dongle. Everything proceeds.

- **Hub restarts while remote is running**: hub comes up with empty state. Remote is already connected. Two sub-cases:
  - Remote has a live WebSocket: the hub's WebSocket listener comes up, remote detects the reconnect and re-sends the auth handshake. Hub re-registers. ✓
  - Remote does not yet have a WebSocket: remote is in reconnect backoff. Hub comes up and waits. Remote connects, sends auth. ✓

- **Both restart simultaneously**: first to come up waits for the other. The auth handshake on first successful WebSocket connection establishes state on both sides. Order doesn't matter.

### The key design principle

**The auth handshake is always sent on every connection, not just first connection.** The hub always processes it as a (potentially idempotent) registration. The remote always re-sends it after any reconnect, including if the hub side dropped the connection. This makes the system order-independent.

### Remote-initiated vs hub-initiated connections

The current design has the remote connecting out to the hub (`ws://hub-host:port`). This works well when the hub is reachable from the remote.

For mesh or WAN deployments where the remote is behind NAT or a restrictive network, an `"incoming"` mode is also supported: the hub listens and the remote connects to it. Both modes use identical auth and sequencing. The connectivity direction is the only difference.

### HID handshake ownership

After the WebSocket auth completes, the hub runs the full 5-step HID handshake with the dongle (via the relay). This means:
- If the hub restarts, it runs the handshake again on reconnect
- If the relay's buffer contains handshake frames from before the disconnect, those are replayed first, and the hub's handshake follows — this is redundant but harmless (the dongle accepts re-inquiry)
- The relay never needs to run the handshake itself (unlike the ESPHome design, which must do so to get the MAC)

For the Python relay, getting `dongle_mac` for the auth handshake requires the relay to send one command to the dongle (`GET_MAC`, `0x4304`) and read the response before connecting to the hub. This is the only protocol knowledge the relay has. After that, it is fully transparent.

---

## Auth Handshake Protocol

On WebSocket connect, the relay sends a JSON control message before any HID frames:

```json
{
  "type": "auth",
  "token": "YOUR_SECRET",
  "relay_id": "pi-floor2",
  "dongle_mac": "AABBCCDD",
  "fresh_start": false,
  "queue_depth": 47
}
```

- `token`: must match `bridge_token` in the hub's config; connection closed immediately if it doesn't
- `relay_id`: human-readable label, used as the HA device name suffix; defaults to hostname
- `dongle_mac`: obtained by the relay from the dongle before connecting
- `fresh_start`: `true` if the relay just started (queue is empty regardless); `false` if reconnecting
- `queue_depth`: number of buffered frames the relay will replay before going live (0 if `fresh_start`)

Hub responds:

```json
{"type": "auth_ok"}
```

or:

```json
{"type": "auth_fail", "reason": "invalid_token"}
```

After `auth_ok`, if `queue_depth > 0` the relay sends all buffered frames as binary WebSocket messages (each message = one HID frame), then sends:

```json
{"type": "replay_done"}
```

Hub resumes normal operation. HID frames and JSON control messages share the same WebSocket connection; they are distinguished by message type (binary = HID frame, text = control message).

---

## Deployment

### Docker (recommended)

```yaml
# docker-compose.yml on the remote machine
services:
  ws2m-remote:
    image: ghcr.io/raetha/ws2m-remote:latest
    devices:
      - /dev/hidraw0:/dev/hidraw0   # or use device auto-detection
    environment:
      WS2M_HUB_URL: "ws://192.168.1.10:8765"
      WS2M_BRIDGE_TOKEN: "YOUR_SECRET"
      WS2M_RELAY_ID: "floor2"
      # WS2M_DEVICE: "auto"  # default; scans for VID 1a86 / PID e024
    restart: unless-stopped
```

Docker is the preferred deployment method — no dependency management, consistent environment, easy updates (`docker pull` + `docker compose up -d`).

### Native (Linux / Raspberry Pi OS)

```bash
pip install ws2m-remote
ws2m-remote --hub-url ws://192.168.1.10:8765 --token YOUR_SECRET --relay-id floor2
```

A systemd unit file is provided in `remote/ws2m-remote.service`.

### Hub configuration

```yaml
# hub/config.yaml
bridge_token: "YOUR_SECRET"     # required; same value as WS2M_BRIDGE_TOKEN on remotes
bridge_port: 8765               # WebSocket listener port (default 8765)

# usb_dongle is unchanged for local-only setups:
usb_dongle: auto

# For mixed local + remote:
usb_dongle:
  - "/dev/hidraw0"              # local dongle
  - "incoming"                  # accept authenticated remote connections on bridge_port
```

The `"incoming"` value tells the hub to accept incoming relay connections. Remotes register automatically on first auth — no per-remote configuration in `config.yaml`.

---

## Remote Lifecycle in HA

### Auto-registration

On first authenticated connection, the hub creates:
- `data/dongles/<dongle_mac>/sensors.yaml` and `state.yaml`
- HA MQTT discovery device: "WyzeSense Dongle AABBCCDD (floor2)" linked to the service device

### Cleanup

Same as local dongles: the "Cleanup removed dongles" button diffs known dongle MACs against currently-connected dongles (local and remote) and clears topics + data for any that are absent. `mqtt_tool remove-dongle <mac>` works identically.

---

## Implementation Scope

1. **Repo restructure** — rename `wyzesense2mqtt/` → `hub/`, create `remote/`, update imports, CI, Dockerfiles — ~0.5 session
2. **Transport abstraction in `hub/dongle_protocol.py`** — `LocalTransport`, `WebSocketClientTransport`, update `open_dongle()` — ~1 session
3. **`remote/relay.py`** — GET_MAC init, ring buffer queue, auth handshake, bidirectional frame forwarding, reconnect loop — ~0.5 session
4. **Hub WebSocket listener** — accept connections, auth, idempotent re-registration, replay handling — ~0.5 session
5. **Config changes** — `bridge_token`, `bridge_port`, `"incoming"` mode in `usb_dongle` — ~0.25 session
6. **Tests** — mock WebSocket transport, auth/reconnect scenarios, replay, race conditions — ~1 session
7. **Docker + CI** — `remote/Dockerfile`, `remote_release.yml`, `examples/remote/` — ~0.25 session
8. **Docs** — update README, cli_tools.md, deployment guide — ~0.25 session

**Total: ~4.25 sessions.** Step 1 (restructure) is a prerequisite for everything else and should be a standalone PR. The transport abstraction (step 2) is the most invasive change to existing hub code.
