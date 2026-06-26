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

# Stable test dongle MAC used wherever a dongle MAC is needed
TEST_DONGLE_MAC = "DONGLE01"
TEST_SERVICE_ID = "test-service-uuid-1234"


@pytest.fixture()
def tmp_config_dir(tmp_path, monkeypatch):
    """Redirect config.CONFIG_DIR to a temporary directory for the duration of a test."""
    import config as cfg_module

    monkeypatch.setattr(cfg_module, "CONFIG_DIR", str(tmp_path / "config"))
    os.makedirs(str(tmp_path / "config"), exist_ok=True)

    return tmp_path


@pytest.fixture()
def tmp_dongle_dir(tmp_config_dir):
    """Create <config_dir>/dongles/<TEST_DONGLE_MAC>/ and return the path."""
    import config as cfg_module

    dongle_dir = cfg_module.dongle_data_path(TEST_DONGLE_MAC)
    os.makedirs(dongle_dir, exist_ok=True)
    return dongle_dir


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
        "self_topic_root": "wyzesense2mqtt",
        "hass_topic_root": "homeassistant",
        "hass_discovery": True,
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
    """Build a correctly-framed and checksummed wire packet."""
    import struct

    pkt = struct.pack(">HB", 0xAA55, cmd_type)
    pkt += struct.pack("BB", len(payload) + 3, cmd_id)
    pkt += payload
    checksum = sum(pkt) & 0xFFFF
    pkt += struct.pack(">H", checksum)
    return pkt


def make_alarm_payload(
    mac: str = "AAAAAAAA",
    event: int = 0xA2,
    sensor_type: int = 0x01,
    battery: int = 80,
    die_temp: int = 20,
    state: int = 1,
    signal_strength: int = 50,
    timestamp_ms: int = 1_700_000_000_000,
) -> bytes:
    import struct

    header = struct.pack(">QB8sB", timestamp_ms, event, mac.encode("ascii"), sensor_type)
    body = struct.pack(">BBBBBHB", die_temp, battery, 0x00, 0x00, state, 0x0001, signal_strength)
    return header + body


def make_climate_payload(
    mac: str = "CCCCCCCC",
    event: int = 0xE8,
    sensor_type: int = 0x07,
    battery: int = 90,
    die_temp: int = 18,
    temp_hi: int = 22,
    temp_lo: int = 50,
    humidity: int = 55,
    signal_strength: int = 60,
    timestamp_ms: int = 1_700_000_001_000,
) -> bytes:
    import struct

    header = struct.pack(">QB8sB", timestamp_ms, event, mac.encode("ascii"), sensor_type)
    # 10-byte body: [0]=die_temp [1]=battery [2]=unk [3]=unk [4]=temp_hi [5]=temp_lo
    #               [6]=humidity [7]=unk [8]=seq [9]=signal_strength
    body = struct.pack(">BBBBBBBBBB", die_temp, battery, 0x00, 0x00, temp_hi, temp_lo, humidity, 0x00, 0x01, signal_strength)
    return header + body


def make_leak_v2_payload(
    mac: str = "DDDDDDDD",
    event: int = 0xEA,
    sensor_type: int = 0x03,
    battery: int = 75,
    state: int = 0,
    probe_state: int = 0,
    probe_available: int = 1,
    signal_strength: int = 45,
) -> bytes:
    import struct

    header = struct.pack(">B8sB", event, mac.encode("ascii"), sensor_type)
    body = struct.pack(
        ">BBBBBBBBBBB",
        0x00, 0x00, battery, 0x00, 0x00,
        state, probe_state, probe_available,
        0x00, 0x01, signal_strength,
    )
    return header + body


def make_keypad_hms_payload(
    mac: str = "KPADKPAD",
    sensor_type: int = 0x05,
    sub_event: int = 0x02,
    state_byte: int = 0x01,
    battery: int = 100,
    signal_strength: int = 40,
    pin: str = "",
) -> bytes:
    import struct

    header = struct.pack(">B8sB", 0x00, mac.encode("ascii"), sensor_type)
    pin_bytes = pin.encode("ascii") if pin else b""
    total_len = 6 + len(pin_bytes)
    padding = b"\x00" * 4
    data = struct.pack(">B", total_len) + padding + struct.pack(">BBB", sub_event, state_byte, battery)
    data += struct.pack(">B", signal_strength) + pin_bytes
    return header + data
