"""
Smoke tests for the CLI tools.

These only exercise the argument parsers — not the command logic, which is
already covered through the underlying modules (SensorRegistry, MqttGateway,
etc.).  The goal is to confirm that the parsers are wired up correctly: valid
invocations don't error, invalid ones do, and --help exits cleanly.
"""

import pytest

# ---------------------------------------------------------------------------
# cli/dongle_tool.py
# ---------------------------------------------------------------------------


def test_dongle_tool_help_exits_zero():
    from unittest.mock import patch

    import cli.dongle_tool as dt

    with pytest.raises(SystemExit) as exc:
        with patch("sys.argv", ["dongle_tool", "--help"]):
            dt.build_parser().parse_args(["--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("cmd", ["list", "pair", "fix", "monitor"])
def test_dongle_tool_subcommands_parse(cmd):
    import cli.dongle_tool as dt

    args = dt.build_parser().parse_args([cmd])
    assert args.command == cmd


def test_dongle_tool_unpair_requires_mac():
    import cli.dongle_tool as dt

    with pytest.raises(SystemExit):
        dt.build_parser().parse_args(["unpair"])


def test_dongle_tool_unpair_accepts_multiple_macs():
    import cli.dongle_tool as dt

    args = dt.build_parser().parse_args(["unpair", "AAAAAAAA", "BBBBBBBB"])
    assert args.mac == ["AAAAAAAA", "BBBBBBBB"]


def test_dongle_tool_chime_parses_all_args():
    import cli.dongle_tool as dt

    args = dt.build_parser().parse_args(["chime", "AAAAAAAA", "3", "2", "5"])
    assert args.mac == "AAAAAAAA"
    assert args.ring_id == "3"
    assert args.repeat_count == "2"
    assert args.volume == "5"


def test_dongle_tool_raw_parses_bytes():
    import cli.dongle_tool as dt

    args = dt.build_parser().parse_args(["raw", "aa,55,43"])
    assert args.bytes == "aa,55,43"


def test_dongle_tool_device_default():
    import cli.dongle_tool as dt

    args = dt.build_parser().parse_args(["list"])
    assert args.device == "auto"


def test_dongle_tool_device_override():
    import cli.dongle_tool as dt

    args = dt.build_parser().parse_args(["--device", "/dev/hidraw1", "list"])
    assert args.device == "/dev/hidraw1"


def test_dongle_tool_unknown_command_exits_nonzero():
    import cli.dongle_tool as dt

    with pytest.raises(SystemExit) as exc:
        dt.build_parser().parse_args(["notacommand"])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# cli/mqtt_tool.py
# ---------------------------------------------------------------------------


def test_mqtt_tool_help_exits_zero():
    import sys

    import cli.mqtt_tool as mt

    with pytest.raises(SystemExit) as exc:
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(sys, "argv", ["mqtt_tool", "--help"])
            mt.build_parser().parse_args(["--help"])
    assert exc.value.code == 0


def test_mqtt_tool_cleanup_discovery_default_args():
    import cli.mqtt_tool as mt

    args = mt.build_parser().parse_args(["cleanup-discovery"])
    assert args.command == "cleanup-discovery"
    assert args.apply is False
    assert args.listen_seconds == 5


def test_mqtt_tool_cleanup_discovery_apply_flag():
    import cli.mqtt_tool as mt

    args = mt.build_parser().parse_args(["cleanup-discovery", "--apply"])
    assert args.apply is True


def test_mqtt_tool_cleanup_discovery_listen_seconds():
    import cli.mqtt_tool as mt

    args = mt.build_parser().parse_args(["cleanup-discovery", "--listen-seconds", "15"])
    assert args.listen_seconds == 15


def test_mqtt_tool_remove_dongle_parses_mac():
    import cli.mqtt_tool as mt

    args = mt.build_parser().parse_args(["remove-dongle", "AABBCCDD"])
    assert args.command == "remove-dongle"
    assert args.mac == "AABBCCDD"
    assert args.apply is False


def test_mqtt_tool_remove_dongle_apply_flag():
    import cli.mqtt_tool as mt

    args = mt.build_parser().parse_args(["remove-dongle", "AABBCCDD", "--apply"])
    assert args.apply is True


def test_mqtt_tool_no_command_exits_nonzero():
    import cli.mqtt_tool as mt

    with pytest.raises(SystemExit) as exc:
        mt.build_parser().parse_args([])
    assert exc.value.code != 0


def test_mqtt_tool_unknown_command_exits_nonzero():
    import cli.mqtt_tool as mt

    with pytest.raises(SystemExit) as exc:
        mt.build_parser().parse_args(["not-a-command"])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# cli/mqtt_tool.py — discovery topic identification
# ---------------------------------------------------------------------------


class TestIsOurTopic:
    def _v2_payload(self, mac):
        return {"components": {"state": {"unique_id": f"wyzesense_{mac}_state"}}}

    def test_recognizes_v2_device_topic(self):
        """v2 sensor discovery topics use device_id ws2m_sensor_<mac>."""
        from cli.mqtt_tool import _is_our_topic

        topic = "homeassistant/device/ws2m_sensor_AABBCCDD/config"
        assert _is_our_topic(topic, "ws2m_sensor_AABBCCDD", self._v2_payload("AABBCCDD")) == "AABBCCDD"

    def test_recognizes_v1_per_entity_topic(self):
        from cli.mqtt_tool import _is_our_topic

        topic = "homeassistant/sensor/wyzesense_AABBCCDD/battery/config"
        payload = {"unique_id": "wyzesense_AABBCCDD_battery"}
        assert _is_our_topic(topic, "wyzesense_AABBCCDD", payload) == "AABBCCDD"

    def test_rejects_bridge_device(self):
        from cli.mqtt_tool import _is_our_topic

        topic = "homeassistant/binary_sensor/wyzesense_bridge_AABBCCDD/connection_state/config"
        assert _is_our_topic(topic, "wyzesense_bridge_AABBCCDD", {}) is None

    def test_rejects_foreign_device(self):
        from cli.mqtt_tool import _is_our_topic

        assert _is_our_topic("homeassistant/device/zigbee2mqtt_x/config", "zigbee2mqtt_x", {}) is None

    def test_rejects_mismatched_unique_ids(self):
        from cli.mqtt_tool import _is_our_topic

        payload = {"components": {"state": {"unique_id": "someone_else_entirely"}}}
        topic = "homeassistant/device/ws2m_sensor_AABBCCDD/config"
        assert _is_our_topic(topic, "ws2m_sensor_AABBCCDD", payload) is None


class TestSchemaFromTopic:
    def test_v2_device_topic(self):
        from cli.mqtt_tool import _schema_from_topic

        assert _schema_from_topic("homeassistant/device/ws2m_sensor_AABBCCDD/config", "homeassistant") == "v2"

    def test_v1_per_entity_topic(self):
        from cli.mqtt_tool import _schema_from_topic

        result = _schema_from_topic("homeassistant/sensor/wyzesense_AABBCCDD/battery/config", "homeassistant")
        assert result.startswith("v1")

    def test_respects_custom_hass_root(self):
        from cli.mqtt_tool import _schema_from_topic

        assert _schema_from_topic("ha/discovery/device/ws2m_sensor_X/config", "ha/discovery") == "v2"
