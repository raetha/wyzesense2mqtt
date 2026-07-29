# Backlog

Known issues and design items that are worth tackling eventually but don't
warrant a patch release on their own — either because they need more design
thought, touch more of the codebase than a contained fix, or represent an
accepted trade-off that's fine for now but worth revisiting.

This is not a roadmap or a commitment to a version; items get pulled from
here into a real changelog entry (`Added`/`Fixed`/`Security`, per the usual
rules) once someone actually works on them, and should be removed from this
file at that point rather than left stale.

---

## Security / hardening

### Raw sensor/dongle MAC bytes used unsanitized in MQTT topics

**Where:** throughout `hub/mqtt.py` — every topic is built with an f-string
like `f"{self_root}/sensor/{sensor_mac}/..."`.

**Issue:** `is_valid_mac()` (`hub/sensors.py`) only checks length (8) and
excludes a few known all-zero garbage patterns — it doesn't check that the
MAC is made up of safe characters. Real hardware MACs are decoded `ascii`
first, falling back to `latin-1` for non-ASCII bytes
(`shared/dongle_protocol.py`), so a MAC can legitimately contain arbitrary
byte values 0x00–0xFF. A MAC containing `/`, `+`, or `#` would inject extra
MQTT topic levels or wildcards the same way the 4.0.1 `remote_id` issue did.

**Why deferred:** exploiting this requires spoofing a WyzeSense sensor's
paired MAC over RF (or a genuinely corrupt/malfunctioning dongle NVRAM) — a
much higher bar than sending a crafted JSON message to an open TCP port, which
is what made the `remote_id` case urgent enough for a patch. Fixing it well
also isn't a one-line change: MACs are used as dict keys, HA `unique_id`
components, and topic segments all over the codebase, and any fix needs to
preserve uniqueness/identity while making topics safe (e.g. hex-encoding, or
tightening `is_valid_mac()` to a real hex check and rejecting/quarantining
anything else at ingestion). That's a broader, more careful pass than a
patch release should carry.

**Possible approach:** tighten `is_valid_mac()` to require actual hex
characters (or otherwise a known-safe charset) rather than just length +
non-zero, and decide what happens to a MAC that fails validation — reject the
event, or sanitize/escape it for use in topics while keeping the raw value
for logging.

### No TLS on the remote↔hub WebSocket (`ws://`, not `wss://`)

**Where:** `hub/ws_listener.py` (server), `remote/remote.py` (client),
`design_remote_bridge.md`.

**Issue:** the remote bridge auth token is sent in cleartext on every
handshake (not just once), over `ws://`. Anyone who can passively observe
traffic on the same network segment can capture a valid token and impersonate
that remote indefinitely (until the token is rotated by removing/re-adopting
the remote).

**Why deferred:** this is consistent with the project's existing threat model
— the whole remote bridge design assumes a trusted home LAN, same as the
plaintext MQTT broker connection most users already run. Adding TLS is a
real feature (certificate management story, config surface, docs, and
probably a `wss://` opt-in rather than a default) rather than a contained
fix, and isn't blocking anyone's actual usage today.

**Possible approach:** support `wss://` as an opt-in `hub_url` scheme on the
remote side and an optional cert/key pair on the hub's listener config;
self-signed cert generation or a Let's Encrypt story would need its own
design pass.

---

## Infrastructure / process

### Docker images run as root; some dependencies use unpinned ranges

**Where:** `docker/Dockerfile.hub`, `docker/Dockerfile.remote`,
`hub/requirements.txt`, `remote/requirements.txt`.

**Issue:** neither Dockerfile has a `USER` directive, so both containers run
as root. `PyYAML`, `retrying`, `websockets >= 12`, and `zeroconf>=0.131` have
no upper bound, so a build can pick up a new major version unexpectedly
(or, in the worst case, a compromised release of a dependency).

**Why deferred:** running as root is a long-standing, common trade-off for
containers that need direct USB device access (`/dev/hidrawN`); moving off
it would need a proper device-cgroup-rule / group-permissions story, not
just adding `USER`. Pinning dependency versions is a process decision
(trades automatic security-patch pickup for reproducible builds) rather than
a bug fix — worth deciding deliberately rather than as a drive-by change.

**Possible approach:** for root — investigate a dedicated non-root user with
device cgroup rules granting access to the specific dongle device node. For
dependency pinning — consider `pip-compile`/lockfile-style pinning with a
scheduled Dependabot-driven bump, so versions are explicit but still get
patched automatically via PRs.

---

## Non-security items already known but not yet scheduled

These were already tracked in project notes before this file existed; moved
here so there's one place to look.

- **ESPHome remote bridge** — design doc exists
  (`docs/design_remote_bridge_esphome.md`), implementation not started.
  Generic `usb_hid_host` / `websocket_client` ESPHome components are planned
  as separate repos; the ws2m-specific `wyzesense_bridge` component stays in
  this repo.
- **Lightweight HA UI for config management** — noted as in-scope for a
  future release; not started.
- **HA-configurable remote queue knobs** (`WS2M_QUEUE_MAX_SECONDS` /
  `MAX_FRAMES` / `HANDSHAKE_FRAMES`) — deliberately env-only for now (expert
  transport tuning); the `set_*` control-frame + `save_setting` pattern used
  for `set_dongle`/`set_log_level` is available to reuse if this changes.
