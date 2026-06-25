# Wyze Sense V2 Keypad

The Wyze Sense V2 Keypad (`WSDPAD1`) is supported as a bridge device. This
page explains what ws2m publishes when the keypad is active, and how to use
that data in Home Assistant — either with raw automations or with the
[Alarmo](https://github.com/nielsfaber/alarmo) alarm integration.

---

## How the keypad is bridged

ws2m is a **pure bridge** — it publishes exactly what the keypad reports over
the USB dongle, with no internal state machine or countdown timers of its own.
All alarm logic (entry delays, exit delays, arming sequences, PIN validation
decisions) belongs in Home Assistant or Alarmo.

The keypad sends four distinct event types:

| Event | When | What ws2m publishes |
|---|---|---|
| **Mode** | User presses a mode button (Away, Home, Off/Disarm, Alarm) | `alarm_mode` = `armed_away` / `armed_home` / `disarmed` / `triggered` |
| **Motion** | PIR sensor on the keypad face fires | `motion` = `active` / `inactive` |
| **PIN start** | User starts entering a PIN (no digits yet) | Logged only; no MQTT publish |
| **PIN confirm** | User finishes entering a PIN | `pin_valid` = `true`/`false` on `…/<mac>/pin` |

---

## MQTT topics

All topics use the configured `self_topic_root` (default `ws2m`).
Replace `<mac>` with the keypad's 8-character MAC address.

### State topic

```
ws2m/<mac>
```

ws2m publishes JSON here on every mode or motion event. The payload always
contains the field relevant to that event type, plus battery and signal
diagnostics.

**Mode event example:**
```json
{
  "alarm_mode": "armed_away",
  "battery": 87,
  "signal_strength": -55
}
```

**Motion event example:**
```json
{
  "motion": "active",
  "battery": 87,
  "signal_strength": -55
}
```

### PIN topic

```
ws2m/<mac>/pin
```

Published on every PIN confirm event. PIN validation against your configured
PIN list happens in ws2m; the result is published here for automations to act
on.

```json
{
  "pin_valid": true,
  "battery": 87,
  "signal_strength": -55
}
```

> **Security note:** ws2m does not publish the PIN digits themselves, only
> `pin_valid`. The raw digits never appear in the MQTT payload.

### Command topic

```
ws2m/<mac>/set
```

ws2m subscribes to this topic so that Home Assistant or Alarmo can reflect the
current alarm state back to the keypad's discovery entity. Send one of:
`disarmed`, `armed_home`, `armed_away`, `triggered`.

### Availability topic

```
ws2m/<mac>/status          →  "online" / "offline"   (sensor heartbeat)
ws2m/dongle_<mac>/status   →  "online" / "offline"   (dongle connectivity)
```

The keypad is marked unavailable in HA if either topic goes offline
(`availability_mode: all`). The dongle topic goes offline if the USB dongle
disconnects or the bridge stops.

---

## Home Assistant discovery

When Home Assistant discovery is enabled, ws2m publishes a device-based config
topic that creates the following entities automatically:

| Entity | Type | Notes |
|---|---|---|
| Alarm control panel | `alarm_control_panel` | Reflects `alarm_mode`; accepts commands |
| Motion | `binary_sensor` (motion) | From the keypad's built-in PIR |
| PIN count | `sensor` | Number of PINs currently configured |
| Arm PIN capture | `button` | Arms ws2m to capture the next hardware PIN entry |
| Clear all PINs | `button` | Removes all configured PINs from `sensors.yaml` |
| Sensor name | `text` | Rename the keypad device in HA |
| Battery | `sensor` (battery %) | Diagnostic |
| Signal strength | `sensor` (dBm) | Diagnostic; disabled by default |
| Remove sensor | `button` | Config button to unpair |

---

## Configuring PINs

PINs can be managed from either the Home Assistant device page or by editing `sensors.yaml` directly.

### Managing PINs from Home Assistant (recommended)

Each keypad device in HA has three PIN management entities:

- **PIN count** — a sensor showing how many PINs are currently configured.
- **Arm PIN capture** — press this button to arm ws2m for capture, then enter the new PIN on the physical keypad within the next keypress cycle. ws2m adds it to the configured list automatically and updates the PIN count.
- **Clear all PINs** — removes every configured PIN from `sensors.yaml`. After clearing, all PIN entries are treated as valid until new PINs are added.

### Managing PINs via sensors.yaml

Add a `pins` key to the keypad entry in `sensors.yaml`. This controls which
PINs are considered valid when ws2m publishes `pin_valid` to the PIN topic.

```yaml
KPADKPAD:
  name: Front Door Keypad
  sensor_type: keypad
  pins:
    - "1234"
    - "5678"
```

If `pins` is omitted or empty, **all PIN entries are treated as valid**
(`pin_valid: true`). This is useful if you want to handle PIN logic entirely
in HA automations.

A single PIN can also be given as a plain string rather than a list:

```yaml
pins: "1234"
```

> **Note:** PIN digits are stored as strings. Use quoted values (`"1234"`, not `1234`) to avoid YAML treating leading zeros as octal.

---

## Using Alarmo (recommended)

[Alarmo](https://github.com/nielsfaber/alarmo) is a full-featured HA alarm
panel integration that handles everything the keypad expects: entry delays,
exit delays, arming sequences, sirens, and notifications. It integrates
cleanly with ws2m because it uses the same `alarm_control_panel` MQTT
interface.

### Basic setup

1. Install Alarmo via HACS (or manually).
2. Open **Alarmo → Configuration → MQTT**.
3. Set the **state topic** to `ws2m/<mac>` and the
   **command topic** to `ws2m/<mac>/set`.
4. Set the **state payload** field to use `alarm_mode` from the JSON:

   ```
   {{ value_json.alarm_mode }}
   ```

5. Under **Alarmo → Configuration → Codes**, add the PINs that should disarm
   the system. You can do PIN validation entirely in Alarmo and skip the
   `pins` key in `sensors.yaml` (set `pins` to empty or omit it).

### Entry and exit delays

Alarmo handles these natively — no configuration in ws2m is needed. When
Alarmo is arming, it publishes an intermediate `arming` state to the command
topic, which ws2m reflects on the state topic. When it is counting down an
entry delay, it publishes `pending`. The keypad display is driven by whatever
state HA publishes back via the command topic.

### Triggering the alarm

The keypad has a physical panic/alarm button that sends `alarm_mode: triggered`
to the state topic. In Alarmo, create a trigger rule on the
`alarm_control_panel` entity reaching `triggered` state to sound your siren or
send notifications.

---

## Using HA automations (without Alarmo)

If you prefer not to use Alarmo, the same keypad data is accessible via MQTT
topics and the HA `mqtt` integration.

### Arming Away on keypad press

```yaml
alias: Keypad – Arm Away
trigger:
  - platform: mqtt
    topic: ws2m/<mac>
    value_template: "{{ value_json.alarm_mode }}"
    payload: "armed_away"
condition: []
action:
  - service: alarm_control_panel.alarm_arm_away
    target:
      entity_id: alarm_control_panel.my_alarm
```

### Disarming on valid PIN

ws2m publishes `pin_valid: true` when the keypad user enters a PIN that
matches your `sensors.yaml` list. Use this to trigger disarming:

```yaml
alias: Keypad – Disarm on valid PIN
trigger:
  - platform: mqtt
    topic: ws2m/<mac>/pin
    value_template: "{{ value_json.pin_valid }}"
    payload: "True"
condition: []
action:
  - service: alarm_control_panel.alarm_disarm
    target:
      entity_id: alarm_control_panel.my_alarm
```

### Entry delay (manual)

If you want a 30-second entry delay before triggering the alarm, use a
`wait_for_trigger` or a counter helper:

```yaml
alias: Keypad – Entry delay before alarm
trigger:
  - platform: state
    entity_id: alarm_control_panel.my_alarm
    to: "triggered"
action:
  - delay:
      seconds: 30
  - condition: state
    entity_id: alarm_control_panel.my_alarm
    state: "triggered"   # still triggered after the delay? then fire.
  - service: notify.mobile_app_phone
    data:
      message: "Alarm triggered!"
```

> Alarmo handles all of this automatically. Manual automations are best suited
> for simple setups where you do not need countdown timers on the keypad display.

---

## Known limitations and future work

### Keypad display feedback — partially confirmed

When HA or Alarmo sends a command to the keypad command topic (e.g. after
arming), ws2m forwards that state to the dongle using `CMD_SEND_KEYPAD_EVENT`
(`0x53/0x53`), which the dongle relays to the keypad over RF.  This should
update the keypad's indicator LEDs and display to reflect the current alarm
state.

The packet structure for this command was confirmed from
[AK5nowman/WyzeSense](https://github.com/AK5nowman/WyzeSense), a C# library
that reverse engineered the dongle protocol using Ghidra.  The state byte
mapping used is:

| HA state | State byte |
|---|---|
| `disarmed` | `0x01` |
| `armed_home` | `0x02` |
| `armed_away` | `0x03` |
| `triggered` | `0x05` |

What has **not** been confirmed with physical hardware is whether this command
also triggers audible feedback (beep/tone) on the keypad speaker, or only
drives the LEDs.  If you have a keypad and can test this, please open a GitHub
issue with your findings — see
[`docs/contributing_protocol.md`](contributing_protocol.md) for what to
include.

### `CMD_PLAY_CHIME` does not apply to the keypad

The `CMD_PLAY_CHIME` command (`0x53/0x70`) sends audio to paired **Wyze Sense
Chime** accessories (the plug-in chime unit), not to the keypad.  These are
separate device types and the commands are unrelated.


**The keypad shows up as `unknown` type in HA.**  
Edit `sensors.yaml` and set `sensor_type: keypad` for the keypad MAC, then
send a Reload command or restart the bridge.

**`pin_valid` is always `false`.**  
Check that the `pins` list in `sensors.yaml` contains strings, not integers:
`- "1234"` not `- 1234`. YAML bare numbers will not match the keypad's ASCII
digit stream. You can verify the current PIN count from the **PIN count** sensor
entity on the HA device page.

**Motion events stop arriving.**  
The keypad PIR has a cooldown period of roughly 60 seconds between `active`
events. If the sensor is quiet for more than 4 hours, ws2m will mark it
offline — this is normal availability timeout behaviour and does not affect
the alarm control panel entity.
