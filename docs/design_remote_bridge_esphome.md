# Design: Remote Bridge (ESPHome / ESP32)

This document covers the ESP32-based remote bridge. For the Python/Docker implementation see [`docs/design_remote_bridge.md`](design_remote_bridge.md).

---

## What ESPHome Can and Can't Do Natively

Before deciding on architecture, it's worth being precise about what stock ESPHome provides vs. what requires custom code.

| Capability | ESPHome native | Notes |
|---|---|---|
| Wi-Fi connection | ✓ | `wifi:` component |
| HA auto-registration | ✓ | `api:` component (native API) |
| OTA firmware updates | ✓ | `ota:` component |
| Sensor/button entities in HA | ✓ | Standard components |
| HTTP client | ✓ | `http_request:` component |
| **WebSocket client** | ✗ | Not a standard component as of mid-2025 |
| **USB Host (HID device)** | ✗ | Not a standard component |
| Inline C++ in YAML | ✓ | `lambda:`, `custom_component:`, local `.h` includes |
| External component (from GitHub) | ✓ | `external_components: source: github://...` |

**Conclusion:** Two pieces require custom code — the WebSocket client and the USB Host HID driver. Everything else (Wi-Fi, HA registration, OTA, status entities) is stock ESPHome and requires no custom code. The question is how much custom C++ is needed and how it's packaged.

---

## Hardware Requirements

### Compatible chips

USB Host mode (the ESP32 must be the *host*, the dongle is the *device*) is only available on:

| Chip | USB Host | Notes |
|---|---|---|
| ESP32-S3 | ✓ (USB OTG) | **Recommended** |
| ESP32-S2 | ✓ (USB OTG) | Fewer GPIO, older |
| ESP32-P4 | ✓ | Newer, higher cost |
| ESP32 / C3 / C6 / H2 | ✗ | No USB Host support |

**The standard ESP32 (original, WROOM/WROVER) does not support USB Host.** Many users have these — they cannot be used for this purpose.

### Recommended board

**ESP32-S3 DevKitC-1** — widely available, ~$10, has two USB ports (one for programming via onboard USB-UART, one USB OTG via GPIO 19/20). The OTG port needs a USB-A female socket wired in, or a USB-A breakout board.

Alternatively, any ESP32-S3 board that exposes the OTG USB data lines works. The Adafruit Feather ESP32-S3 and the LilyGO T-Display-S3 both expose OTG USB.

### Wiring

If using a raw USB-A female socket:
```
ESP32-S3 GPIO 19 → USB D-  (white wire)
ESP32-S3 GPIO 20 → USB D+  (green wire)
ESP32-S3 5V/VBus → USB VBus (red wire)  — must supply 5V to the dongle
ESP32-S3 GND     → USB GND  (black wire)
```

A dev board powered by USB-C at 5V can typically supply the dongle (which draws ~50 mA). Check your board's VBus current rating.

---

## Implementation Options

The custom code is split across three components with different reuse scope. The development path and end-user install path differ.

### Development path (local)

During development, all three components are developed locally — `usb_hid_host` and `websocket_client` from local clones of their respective repos, and `wyzesense_bridge` from the ws2m repo:

```yaml
external_components:
  - source:
      type: local
      path: /path/to/esphome-usb-hid-host/components
    components: [usb_hid_host]
  - source:
      type: local
      path: /path/to/esphome-websocket-client/components
    components: [websocket_client]
  - source:
      type: local
      path: remote/esphome/components
    components: [wyzesense_bridge]
```

This lets you develop and test all three components together without publishing anything.

### End-user path (published)

Once the generic components are published to their own GitHub repos, users reference them by individual version tag. See the complete YAML in the next section. Non-ws2m users who only need `usb_hid_host` or `websocket_client` reference only those repos without pulling in anything ws2m-related.

---

## What the Custom Component Does

The component has three responsibilities:

1. **USB HID Host**: open the WyzeSense dongle (VID `1a86`, PID `e024`) using the ESP-IDF USB Host stack and read/write raw 64-byte HID frames.

2. **GET_MAC init**: before connecting to the hub, send the `GET_MAC` command (`0x4304`) to the dongle and read the response. This is the only protocol knowledge the component has — it needs the dongle MAC for the auth handshake. After that, the component is a transparent relay.

3. **WebSocket relay**: connect to the hub's WebSocket listener, send the auth handshake (same JSON protocol as the Python relay), then forward HID frames bidirectionally.

The component does NOT include a ring buffer queue. Flash memory write cycles on the ESP32 are limited (~100k cycles), and the 10-second window that makes sense for the Python relay involves writing every HID frame — impractical on flash. In-RAM buffering is possible but the ESP32's available heap (~300 KB) limits the buffer to a few seconds of traffic at best. Given that the primary failure mode on ESP32 is Wi-Fi re-association (typically 1–3 seconds), a small in-RAM buffer of ~50 frames (~3 seconds) is reasonable and does not require flash writes.

---

## Complete ESPHome YAML

This is the full configuration a user would use. With Option A, the `external_components` block points to a local `components/` directory. With Option B, it points to GitHub.

```yaml
# wyzesense_bridge.yaml

esphome:
  name: wyzesense-bridge-floor2
  friendly_name: "WyzeSense Remote Bridge Floor 2"
  platform: esp32
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf         # required for USB Host; Arduino framework does not support it

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  ap:                     # fallback AP if Wi-Fi is unavailable
    ssid: "ws2m-bridge-fallback"
    password: !secret wifi_ap_password

api:                      # ESPHome native API — auto-registers in HA
  encryption:
    key: !secret api_encryption_key

ota:
  password: !secret ota_password

logger:
  level: INFO

# --- Custom component ---
# --- Custom components ---
external_components:
  - source: github://raetha/esphome-usb-hid-host@v1.0.0
    components: [usb_hid_host]
  - source: github://raetha/esphome-websocket-client@v1.0.0
    components: [websocket_client]
  - source: github://raetha/wyzesense2mqtt//remote/esphome/components@main
    components: [wyzesense_bridge]

wyzesense_bridge:
  hub_url: !secret ws2m_hub_url       # e.g. ws://192.168.1.10:8765
  token: !secret ws2m_bridge_token
  relay_id: "floor2"
  # ram_buffer_frames: 50             # optional; default 50 (~3 seconds of traffic)

# --- HA entities (all from the custom component) ---
text_sensor:
  - platform: wyzesense_bridge
    name: "Bridge Status"
    id: bridge_status
    # Reports: connecting / auth_failed / dongle_not_found / connected / reconnecting

sensor:
  - platform: wyzesense_bridge
    name: "Frames Relayed"
    id: frames_relayed
    unit_of_measurement: "frames"
    accuracy_decimals: 0
    state_class: total_increasing

  - platform: wifi_signal
    name: "WiFi Signal"
    update_interval: 60s

button:
  - platform: restart
    name: "Restart"

binary_sensor:
  - platform: status
    name: "Online"
```

**Secrets file (`secrets.yaml`):**

```yaml
wifi_ssid: "YourNetwork"
wifi_password: "wifi-password"
wifi_ap_password: "fallback-password"
api_encryption_key: "base64-key-from-esphome-dashboard"
ota_password: "ota-password"
ws2m_hub_url: "ws://192.168.1.10:8765"
ws2m_bridge_token: "YOUR_SECRET"
```

All secrets are editable via the ESPHome dashboard without reflashing firmware. The `hub_url` and `token` can be changed OTA.

---

## Component C++ Structure

The custom component is approximately 400–500 lines of C++ across two files. This is the rough structure — sufficient for a developer to understand scope before implementation.

### `components/wyzesense_bridge/__init__.py`
ESPHome Python schema for the YAML keys — ~50 lines.

### `components/wyzesense_bridge/wyzesense_bridge.h`

```cpp
class WyzeSenseBridge : public Component, public EntityBase {
public:
    // ESPHome lifecycle
    void setup() override;
    void loop() override;

    // Config (set from YAML schema)
    void set_hub_url(const std::string &url);
    void set_token(const std::string &token);
    void set_relay_id(const std::string &relay_id);

    // Entity accessors for YAML sensors/text_sensors
    TextSensor *status_sensor{nullptr};
    Sensor *frames_relayed_sensor{nullptr};

private:
    // USB HID
    bool usb_init_();
    bool get_dongle_mac_(std::string &mac_out);
    bool usb_read_(uint8_t *buf, size_t len);
    bool usb_write_(const uint8_t *buf, size_t len);

    // WebSocket
    esp_websocket_client_handle_t ws_client_{nullptr};
    bool ws_connect_();
    bool ws_auth_(const std::string &mac);
    void ws_send_frame_(const uint8_t *frame, size_t len);
    static void ws_event_handler_(void *arg, esp_event_base_t, int32_t, void *);

    // Relay loop
    void relay_loop_();

    // RAM queue (ring buffer)
    struct QueuedFrame { uint32_t ts_ms; uint8_t data[64]; size_t len; };
    std::deque<QueuedFrame> frame_queue_;
    size_t max_queue_frames_{50};

    std::string hub_url_, token_, relay_id_, dongle_mac_;
    enum class State { INIT, USB_WAIT, WS_CONNECTING, AUTH, RELAY, RECONNECT };
    State state_{State::INIT};
};
```

### USB Host implementation notes

The ESP-IDF USB Host API (`usb/usb_host.h`) requires running a USB host library task. The WyzeSense dongle uses a non-standard HID usage page, so the standard HID class driver won't enumerate it. We instead:

1. Install the USB Host library
2. Wait for device connection event (vendor `1a86`, product `e024`)
3. Claim interface 0
4. Transfer on interrupt endpoints directly (IN endpoint for dongle→host, OUT endpoint for host→dongle)

This is the most complex part (~150 lines of C). The ESP-IDF documentation and community examples for USB HID Host provide the pattern. Physical hardware is required to test.

### WebSocket implementation notes

`esp_websocket_client` from ESP-IDF is available when using the `esp-idf` framework (not Arduino). It supports:
- Binary and text message types (needed for distinguishing HID frames from control messages)
- TLS (`wss://`)
- Event-driven callbacks

The `esp-idf` framework type is required in the ESPHome YAML (`framework: type: esp-idf`) — the Arduino framework does not expose USB Host or `esp_websocket_client`. This is already set in the example YAML above.

---

## Differences vs Python Relay

| Feature | Python relay | ESPHome component |
|---|---|---|
| Persistent queue (SQLite) | Planned extension | Not feasible (flash wear) |
| RAM queue | 10-second ring buffer | ~50 frames in-RAM |
| Runs in Docker | Yes | N/A |
| OTA updates | Via CI + Docker pull | Native ESPHome OTA |
| HA presence | Via ws2m hub only | Native (ESPHome API) |
| Hardware cost | Pi Zero W (~$15) | ESP32-S3 dev board (~$10) |
| Setup after initial config | `docker compose up -d` | ESPHome dashboard |
| Initial flash | N/A | USB cable required once |
| USB Host driver risk | N/A (Linux kernel handles it) | Custom C++ required |

---

## Repository Structure

### Generic components — individual repos

The two generic ESPHome components each live in their own repository. This keeps versioning, issues, and release cadence fully independent — a patch to `usb_hid_host` has no effect on `websocket_client` users and vice versa. As you build more unrelated ESPHome components over time, each gets its own repo.

- `github.com/raetha/esphome-usb-hid-host` — USB HID Host for ESP32-S3; not ws2m-specific; strong ESPHome core submission candidate
- `github.com/raetha/esphome-websocket-client` — WebSocket client for ESP32 (esp-idf framework); not ws2m-specific

Users reference each by its own version tag:

```yaml
external_components:
  - source: github://raetha/esphome-usb-hid-host@v1.0.0
    components: [usb_hid_host]
  - source: github://raetha/esphome-websocket-client@v1.0.0
    components: [websocket_client]
```

### ws2m-specific component — main ws2m repo

`wyzesense_bridge` lives in the main `wyzesense2mqtt` repo under `remote/esphome/components/wyzesense_bridge/`. It depends on the two generic components above. Users reference it from the main ws2m repo:

```yaml
external_components:
  - source: github://raetha/esphome-usb-hid-host@v1.0.0
    components: [usb_hid_host]
  - source: github://raetha/esphome-websocket-client@v1.0.0
    components: [websocket_client]
  - source: github://raetha/wyzesense2mqtt//remote/esphome/components@main
    components: [wyzesense_bridge]
```

Keeping `wyzesense_bridge` in the main ws2m repo ensures that hub protocol changes (auth handshake format, queue signals) and the ESPHome component stay in sync — they're developed together and version together.

During local development, all three are referenced by local path:

```yaml
external_components:
  - source:
      type: local
      path: /path/to/esphome-usb-hid-host/components
    components: [usb_hid_host]
  - source:
      type: local
      path: /path/to/esphome-websocket-client/components
    components: [websocket_client]
  - source:
      type: local
      path: remote/esphome/components
    components: [wyzesense_bridge]
```

---

## Implementation Scope

The work is divided across three components:

### Generic components (separate repos, reusable by others)

**`esphome-usb-hid-host`** (~1.5–2 sessions):
1. USB Host library task setup, device enumeration by VID/PID
2. Interface claim, interrupt endpoint transfers (IN/OUT)
3. YAML schema, ESPHome component lifecycle
4. Physical hardware testing

**`esphome-websocket-client`** (~0.75 session):
1. `esp_websocket_client` wrapper (text + binary message types, TLS support)
2. Reconnect handling, event callbacks
3. YAML schema

### ws2m-specific component (in main repo under `remote/esphome/`)

**`wyzesense_bridge`** (~1.5 sessions):
1. GET_MAC init sequence
2. Auth handshake (JSON, same protocol as Python relay)
3. Bidirectional relay loop with in-RAM ring buffer (~50 frames)
4. Status entities (`TextSensor`, `Sensor`)
5. Complete example YAML (`remote/esphome/wyzesense_bridge.yaml`)
6. Integration testing against the hub WebSocket listener

### Supporting work

- CI for generic component repos (~0.25 session each)
- ESPHome component documentation (~0.25 session)

**Total: ~4.5–5.5 sessions.** The USB Host driver is the highest-risk piece — it requires C++ and physical hardware. If USB enumeration proves problematic for this specific dongle, this is where the project could stall.

**Prerequisite:** the hub-side WebSocket listener (from `design_remote_bridge.md`) must be implemented first so the ESP32 has something to connect to.
