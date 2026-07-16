# WyzeSense USB Dongle Protocol Reference

This document is the ws2m project's protocol specification for the WyzeSense USB HID dongle (WHSB1 / CH554 bridge, USB vendor 1a86 / product e024). It is derived from community reverse engineering — there is no official specification.

Primary sources:
- HclX/WyzeSensePy (original Python library and [Rust reimplementation](https://github.com/HclX/wyzesense2mqtt-rs))
- AK5nowman/WyzeSense (C# library, verified via Ghidra)
- PR #63 (drinfernoo): keypad HMS events
- PR #82 (HclX): chime play command

All implementation lives in `hub/dongle_protocol.py`.

---

## 1. HID Transport Layer

The host communicates via raw USB HID on `/dev/hidrawN` (Linux). Dongle discovery uses sysfs matching on vendor `1a86`, product `e024`.

Each `read()` returns exactly one 64-byte HID report:

```
[0]       Length byte — number of valid protocol bytes that follow (max 0x3F = 63)
[1..len]  Protocol bytes — the only valid data; bytes beyond 1+len are stale garbage
```

The first byte is NOT a HID Report ID. Only `data[1:1+length]` is valid. Multiple protocol packets may be concatenated in one HID frame; the receiver maintains a reassembly buffer.

**Write:** raw protocol bytes are written directly — no HID framing required.

---

## 2. Protocol Packet Frame

Every packet:

```
[0..1]  Magic (2B)      — 0xAA55 host→dongle; 0x55AA or 0xAA55 dongle→host
[2]     Command type    — 0x43 Sync (request/reply) or 0x53 Async (event/notify)
[3]     b2              — len(payload)+3 for normal; lower byte of ACK'd cmd for ACK
[4]     Command ID
[5..]   Payload         — size = b2 - 3
[N-2.N] Checksum        — sum(all preceding bytes) & 0xFFFF, big-endian
```

**ACK packets** (`cmd=0x53FF`): always 7 bytes; `b2` holds the lower byte of the acknowledged command.  
**Normal packets:** total length = b2 + 4.

Endianness: big-endian for all multi-byte integers.

---

## 3. Command Reference

### 3.1 Sync Commands (0x43 — host initiated, expects response on cmd+1)

| Command           | Code     | Payload                      | Response  |
|---|---|---|---|
| Inquiry           | `0x4327` | None                         | `0x4328` — `[0x01]` on success |
| Get ENR           | `0x4302` | 16 bytes (`[0x30]*16`)       | `0x4303` — 16-byte ENR token |
| Get MAC           | `0x4304` | None                         | `0x4305` — 8-byte ASCII MAC |
| Get Key           | `0x4306` | None                         | `0x4307` — 16-byte key |
| Update CC1310     | `0x4312` | None                         | `0x4313` |
| CH554 Upgrade     | `0x430E` | None                         | `0x430F` |

### 3.2 Async Commands (0x53 — host→dongle, dongle ACKs then responds)

| Command               | Code     | Payload                                        | Response  |
|---|---|---|---|
| Finish Auth           | `0x5314` | `[0xFF]`                                       | `0x5315` — empty |
| Get Version           | `0x5316` | None                                           | `0x5317` — ASCII version string |
| Start/Stop Scan       | `0x531C` | `[0x01]`=start, `[0x00]`=stop                  | `0x531D` — `[0x01]` on success |
| Get Sensor R1         | `0x5321` | 8-byte MAC + 16-byte R1 token                  | `0x5322` |
| Verify Sensor         | `0x5323` | 8-byte MAC + `[0xFF, 0x04]`                    | `0x5324` — empty |
| Delete Sensor         | `0x5325` | 8-byte ASCII MAC                               | `0x5326` — MAC + `[0xFF]` |
| Get Sensor Count      | `0x532E` | None                                           | `0x532F` — `[count]` |
| Get Sensor List       | `0x5330` | `[count]`                                      | `0x5331` × N — one 8-byte MAC per response |
| Delete All Sensors    | `0x533F` | None                                           | `0x5340` — `[0xFF]` |
| Play Chime            | `0x5370` | 8-byte MAC + `[ring_id, repeat_count, volume]` | `0x5371` — empty |
| Send Keypad Event     | `0x5353` | see §7.3                                       | none |

### 3.3 Async Notifications (0x53 — dongle→host; host must ACK each)

| Notification       | Code     | Description |
|---|---|---|
| Sensor Alarm       | `0x5319` | Standard telemetry (heartbeat, alarm, climate). See §5. |
| Sensor Scan        | `0x5320` | New sensor found during scan. See §4.2. |
| Time Sync Request  | `0x5332` | Dongle requests current time; host replies with `0x5333` + 8-byte ms epoch. |
| Event Log          | `0x5335` | Dongle diagnostic log; host ACKs and may ignore. |
| Sensor Alarm2      | `0x5355` | HMS packet (leak, climate, keypad). See §6. |

---

## 4. Startup Handshake & Sensor Management

### 4.1 Initialisation (5-step unlock)

Before the dongle forwards sensor events the host must complete this sequence:

1. **Inquiry** (`0x4327`) — probe readiness; response payload must be `[0x01]`
2. **Get ENR** (`0x4302`) — send `[0x30]*16`; receive 16-byte ENR token
3. **Get MAC** (`0x4304`) — receive 8-byte ASCII dongle MAC
4. **Get Version** (`0x5316`) — receive ASCII firmware string (e.g. `"0.0.0.47 V1.8 Gateway GW3U"`)
5. **Finish Auth** (`0x5314`) — send `[0xFF]`; empty response confirms unlock

The RF radio is inactive until step 5 completes.

### 4.2 Pairing Flow

1. Enable scan (`0x531C`, `[0x01]`)
2. Wait for `NOTIFY_SENSOR_SCAN` (`0x5320`) — payload:
   - `[0]` = `0xA3` (event marker)
   - `[1..9]` = 8-byte ASCII sensor MAC
   - `[9]` = sensor type byte
   - `[10]` = sensor firmware version byte
3. Get Sensor R1 (`0x5321`) — 8-byte MAC + R1 token `b'Ok5HPNQ4lf77u754'`
4. Disable scan (`0x531C`, `[0x00]`)
5. Verify Sensor (`0x5323`) — 8-byte MAC + `[0xFF, 0x04]` — permanently binds to NVRAM

### 4.3 Sensor List Retrieval

Two-phase: Get Count (`0x532E`) → Get List (`0x5330` with count as payload) → N individual `0x5331` responses each carrying one 8-byte MAC. The count must be sent as the `0x5330` payload.

---

## 5. NOTIFY_SENSOR_ALARM (`0x5319`) — Standard Telemetry

Used for V1 contact, motion, and climate sensors. 18-byte common header followed by event data.

```
[0..7]   Timestamp — big-endian u64 millisecond epoch; divide by 1000 for seconds
[8]      Event type — 0xA1=Heartbeat, 0xA2=Alarm, 0xE8=Climate
[9..16]  Sensor MAC — 8-byte ASCII
[17]     Sensor type — see sensor type table below
[18..]   Event data — structure varies by event type
```

### 5.1 Sensor Type Table

| Code   | Name        | Description |
|---|---|---|
| `0x01` | `switch`    | V1 contact sensor |
| `0x02` | `motion`    | V1 motion sensor |
| `0x03` | `leak`      | V2 leak sensor |
| `0x05` | `keypad`    | V2 HMS keypad |
| `0x07` | `climate`   | V2 climate sensor |
| `0x0C` | `chime`     | RF chime speaker |
| `0x0E` | `switchv2`  | V2 contact sensor |
| `0x0F` | `motionv2`  | V2 motion sensor |

### 5.2 Alarm & Heartbeat Event Data (`0xA1` / `0xA2`) — 8 bytes, format `>BBBBBHB`

```
[0]     Die temperature (°C, signed) — on-chip AON_BATMON:TEMP register
[1]     Battery raw — AON_BATMON:BAT >> 3; convert: voltage_V = raw / 32.0
[2]     Unknown / reserved
[3]     Unknown / reserved
[4]     State — binary sensor state; only meaningful for Alarm (0xA2)
[5..6]  Event sequence — big-endian u16, monotonic counter; does NOT reset on reboot
[7]     Signal strength — unsigned RSSI, dongle-appended; dBm = -raw
```

**State at offset 4, not offset 2.** Earlier documentation had this wrong; confirmed by `struct.unpack_from(">BBBBBHB", data)` from the Python reference.

**Heartbeat (`0xA1`):** State field is not semantically used; die_temp, battery, sequence, and RSSI are valid.

### 5.3 Climate Event Data (`0xE8`) — 10 bytes, format `>BBBBBBBBBB`

```
[0]     Die temperature (°C) — AON_BATMON:TEMP
[1]     Battery raw — same encoding as §5.2
[2]     Unknown
[3]     Unknown
[4]     Temperature integer part (°C)
[5]     Temperature decimal part (÷100 for fractional °C)
[6]     Humidity (%)
[7]     Unknown
[8]     Sequence (1 byte for climate)
[9]     Signal strength — dongle-appended; dBm = -raw
```

**Signal strength is at offset 9, not 7.** Earlier implementations read the wrong byte.

Temperature decoding: `temp_C = temp_hi + (temp_lo / 100.0)`

---

## 6. NOTIFY_SENSOR_ALARM2 (`0x5355`) — HMS Packets

Used for leak, climate V2 events, and keypad. 10-byte header (no timestamp field — use system time):

```
[0]     Event type — 0xEA=Leak, 0xE8=Climate, 0x00=Keypad
[1..8]  Sensor MAC — 8-byte ASCII
[9]     Sensor type
[10..]  Event data
```

### 6.1 Leak Event Data — 11 bytes, format `>BBBBBBBBBBB`

```
[0]     Unknown
[1]     Unknown
[2]     Battery raw — same encoding as §5.2
[3]     Unknown
[4]     Unknown
[5]     State — 0x00=Dry, 0x01=Wet (internal sensor)
[6]     Probe state — 0x00=Dry, 0x01=Wet (external probe)
[7]     Probe available — 0x00=No probe connected, 0x01=Probe connected
[8]     Unknown
[9]     Sequence (1 byte)
[10]    Signal strength — dongle-appended; dBm = -raw
```

### 6.2 Keypad HMS Event Data

Keypad events use sensor type `0x05` and have an internal sub-event structure:

```
[0]     Total payload length (including this byte)
[1..4]  Unknown / padding
[5]     Sub-event type — 0x02=Mode, 0x0A=Motion, 0x06=PIN start, 0x08=PIN confirm, 0x0C=Alarm(?)
[6]     State value (sub-event dependent)
[7]     Raw battery — 0–155 scale (different source from AON_BATMON; normalised to 0–100%)
[8]     Signal strength — dongle-appended; dBm = -raw
[9..]   PIN digits, ASCII (only present for sub-event 0x08; count = data[0] - 6)
```

**Keypad mode state values** (sub-event `0x02`, state byte at `[6]`):

| Raw | HA alarm state  |
|---|---|
| `0x00` | `unknown` — inactive/transient |
| `0x01` | `disarmed` |
| `0x02` | `armed_home` |
| `0x03` | `armed_away` |
| `0x04` | `triggered` |

Cross-referenced from PR #63 (drinfernoo) and AK5nowman/WyzeSense C# — both resolve to the same mapping.

Sub-event `0x0C` is noted in AK5nowman's code as "Some sort of alarm event?" — exact semantics unknown.

---

## 7. Chime — Play Command (`0x5370`)

Payload: 8-byte ASCII MAC + `[ring_id, repeat_count, volume]`

- `ring_id`: `0x00`–`0xFF` — ring tone selection (specific tones not fully documented)
- `repeat_count`: `0x01`–`0xFF` — number of repetitions
- `volume`: `0x01` (quiet) – `0x09` (max); clamped by ws2m

### 7.3 Keypad Feedback Command (`0x5353`)

Drives the keypad display and LEDs to reflect the current alarm system state. Payload layout confirmed from AK5nowman/WyzeSense C#:

```
[0..4]  Fixed header: 0xAA 0x55 0x53 0x0F 0x53
[5..12] 8 zero bytes (padding)
[13]    State byte: 0x01=disarmed, 0x02=armed_home, 0x03=armed_away, 0x04=triggered
[14]    0x00 (trailing)
```

**Unverified with physical hardware:** whether this actually updates the display and/or triggers audio feedback is not confirmed. See `docs/keypad.md`.

---

## 8. Battery Measurement

### 8.1 AON_BATMON Encoding

All sensors except the keypad report battery via the CC1310's `AON_BATMON:BAT` register, right-shifted by 3 bits:

```
voltage_V = raw_byte / 32.0       (0.03125 V per unit)
```

This was previously documented as a percentage — it is not. The raw value happens to be numerically close to percentage for 3V batteries near full charge, but the correct interpretation is voltage.

### 8.2 Per-Sensor Battery Chemistry

| Sensor type | Battery  | Nominal V | Usable range | Notes |
|---|---|---|---|---|
| `switch`    | CR1632   | 3.0V | 2.4–3.2V | |
| `motion`    | CR2450   | 3.0V | 2.4–3.2V | |
| `switchv2`  | 1× AAA   | 1.5V | 0.9–1.6V | AON_BATMON reports at half scale; ws2m doubles raw before /32 |
| `motionv2`  | CR2450   | 3.0V | 2.4–3.2V | |
| `leak`      | CR2450   | 3.0V | 2.4–3.2V | |
| `climate`   | CR2450   | 3.0V | 2.4–3.2V | |
| `chime`     | unknown  | —    | —        | Voltage published; no percentage estimate |
| `keypad`    | separate | —    | 0–155 raw scale (not AON_BATMON) | normalised to 0–100% by ÷155 |

For `switchv2`, the doubling brings the post-doubling range to 1.8–3.2V, matching the 3V coin cell scale for consistent display. The percentage estimate uses a linear discharge curve across the usable range per sensor type. CR2450/CR1632 Li/MnO₂ cells have very flat discharge curves followed by a sharp knee, so the linear approximation is intentionally conservative.

### 8.3 Die Temperature

Offset 0 of alarm, heartbeat, and climate event data is the on-chip die temperature from `AON_BATMON:TEMP`, in °C (signed). This reflects the CC1310 chip temperature, not ambient temperature. Published by ws2m as a disabled-by-default diagnostic entity.

### 8.4 RSSI

Signal strength is dongle-appended — it is not transmitted over the air by the sensor. The dongle measures its own received signal strength and appends the byte before forwarding over USB. The raw value is unsigned; ws2m negates it to produce standard dBm notation (e.g. raw `60` → `-60 dBm`).

---

## 9. Open Items

- **Ring tone IDs**: `ring_id` values for the chime speaker are not fully documented. Known working values: `0x01`–`0x09` at minimum. Full range unknown.
- **Keypad display feedback**: `CMD_SEND_KEYPAD_EVENT` packet structure is confirmed from C# source, but physical effect on the keypad display and speaker has not been verified with hardware.
- **Keypad sub-event `0x0C`**: described as "Some sort of alarm event?" in AK5nowman's code — exact trigger and semantics unknown.

Contributions welcome — see `docs/contributing_hid_captures.md`.
