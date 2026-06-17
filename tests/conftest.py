"""
Shared pytest fixtures for WyzeSense2MQTT tests.
"""


def pytest_addoption(parser):
    """Register --dongle option for hardware tests."""
    parser.addoption(
        "--dongle",
        default="auto",
        metavar="PATH|auto",
        help="Path to the WyzeSense USB HID device for hardware tests, or "
             "'auto' to use the same auto-detection as the bridge (default: auto)",
    )


import os
import sys
import tempfile

import pytest

# Ensure the package directory is on the path for all tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wyzesense2mqtt"))


@pytest.fixture()
def tmp_config_dir(tmp_path, monkeypatch):
    """Redirect config.CONFIG_DIR to a temporary directory for the duration of a test."""
    import config as cfg_module

    monkeypatch.setattr(cfg_module, "CONFIG_DIR", str(tmp_path / "config"))
    os.makedirs(str(tmp_path / "config"), exist_ok=True)

    return tmp_path


@pytest.fixture()
def sample_config(tmp_config_dir):
    """Write a minimal valid config.yaml and return the loaded config dict."""
    import yaml

    import config as cfg_module

    cfg_data = {
        "mqtt_host": "testbroker.local",
        "mqtt_port": 1883,
        "mqtt_username": "user",
        "mqtt_password": "pass",
        "mqtt_client_id": "wyzesense2mqtt_test",
        "mqtt_clean_session": False,
        "mqtt_keepalive": 60,
        "mqtt_qos": 0,
        "mqtt_retain": True,
        "self_topic_root": "wyzesense2mqtt",
        "hass_topic_root": "homeassistant",
        "hass_discovery": True,
        "publish_sensor_name": True,
        "usb_dongle": "auto",
    }
    cfg_path = os.path.join(cfg_module.CONFIG_DIR, cfg_module.MAIN_CONFIG_FILE)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg_data, f)

    return cfg_data


# ---------------------------------------------------------------------------
# Synthetic packet builder helpers (used across multiple test modules)
# ---------------------------------------------------------------------------


def make_packet_bytes(cmd_type: int, cmd_id: int, payload: bytes) -> bytes:
    """Build a correctly-framed and checksummed wire packet.

    Mirrors the logic in Packet.send() so tests can construct valid bytes
    without needing a file descriptor.
    """
    import struct

    pkt = struct.pack(">HB", 0xAA55, cmd_type)
    pkt += struct.pack("BB", len(payload) + 3, cmd_id)
    pkt += payload
    checksum = sum(pkt) & 0xFFFF
    pkt += struct.pack(">H", checksum)
    return pkt


def make_alarm_payload(
    mac: str = "AAAAAAAA",
    event: int = 0xA2,  # EVENT_ALARM
    sensor_type: int = 0x01,  # SENSOR_TYPE_SWITCH
    battery: int = 80,
    state: int = 1,  # 1 = open/active/wet
    signal_strength: int = 50,  # stored positive; negated by SensorEvent.__init__
    timestamp_ms: int = 1_700_000_000_000,
) -> bytes:
    """Build a v1 alarm/heartbeat packet payload (NOTIFY_SENSOR_ALARM inner bytes).

    Layout: >QB8sB then 7 more bytes: pad, battery, pad, pad, state, seq(2B), rssi
    """
    import struct

    header = struct.pack(">QB8sB", timestamp_ms, event, mac.encode("ascii"), sensor_type)
    body = struct.pack(">BBBBBHB", 0x00, battery, 0x00, 0x00, state, 0x0001, signal_strength)
    return header + body


def make_climate_payload(
    mac: str = "CCCCCCCC",
    event: int = 0xE8,  # EVENT_CLIMATE
    sensor_type: int = 0x07,  # SENSOR_TYPE_CLIMATE
    battery: int = 90,
    temp_hi: int = 22,
    temp_lo: int = 50,
    humidity: int = 55,
    signal_strength: int = 60,
    timestamp_ms: int = 1_700_000_001_000,
) -> bytes:
    """Build a v1 climate packet payload."""
    import struct

    header = struct.pack(">QB8sB", timestamp_ms, event, mac.encode("ascii"), sensor_type)
    body = struct.pack(">BBBBBBBB", 0x00, battery, 0x00, 0x00, temp_hi, temp_lo, humidity, signal_strength)
    return header + body


def make_leak_v2_payload(
    mac: str = "DDDDDDDD",
    event: int = 0xEA,  # EVENT_LEAK
    sensor_type: int = 0x03,  # SENSOR_TYPE_LEAK
    battery: int = 75,
    state: int = 0,  # 0 = dry
    probe_state: int = 0,
    probe_available: int = 1,
    signal_strength: int = 45,
) -> bytes:
    """Build a v2 leak packet payload (NOTIFY_SENSOR_ALARM2 inner bytes)."""
    import struct

    header = struct.pack(">B8sB", event, mac.encode("ascii"), sensor_type)
    # Layout: >BBBBBBBBBBB = 11 bytes
    body = struct.pack(
        ">BBBBBBBBBBB",
        0x00, 0x00, battery, 0x00, 0x00,
        state, probe_state, probe_available,
        0x00, 0x01, signal_strength,
    )
    return header + body
