"""
Smoke tests for the CLI tools.

These only exercise the argument parsers — not the command logic, which is
already covered through the underlying modules (SensorRegistry, MqttGateway,
etc.).  The goal is to confirm that the parsers are wired up correctly: valid
invocations don't error, invalid ones do, and --help exits cleanly.
"""

import pytest


# ---------------------------------------------------------------------------
# cli/bridge_tool.py
# ---------------------------------------------------------------------------


def test_bridge_tool_help_exits_zero():
    import sys
    from unittest.mock import patch

    sys.path.insert(0, "wyzesense2mqtt")
    import cli.bridge_tool as bt

    with pytest.raises(SystemExit) as exc:
        with patch("sys.argv", ["bridge_tool", "--help"]):
            bt.build_parser().parse_args(["--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("cmd", ["list", "pair", "fix", "monitor"])
def test_bridge_tool_subcommands_parse(cmd):
    import cli.bridge_tool as bt

    args = bt.build_parser().parse_args([cmd])
    assert args.command == cmd


def test_bridge_tool_unpair_requires_mac():
    import cli.bridge_tool as bt

    with pytest.raises(SystemExit):
        bt.build_parser().parse_args(["unpair"])


def test_bridge_tool_unpair_accepts_multiple_macs():
    import cli.bridge_tool as bt

    args = bt.build_parser().parse_args(["unpair", "AAAAAAAA", "BBBBBBBB"])
    assert args.mac == ["AAAAAAAA", "BBBBBBBB"]


def test_bridge_tool_chime_parses_all_args():
    import cli.bridge_tool as bt

    args = bt.build_parser().parse_args(["chime", "AAAAAAAA", "3", "2", "5"])
    assert args.mac == "AAAAAAAA"
    assert args.ring_id == "3"
    assert args.repeat_count == "2"
    assert args.volume == "5"


def test_bridge_tool_raw_parses_bytes():
    import cli.bridge_tool as bt

    args = bt.build_parser().parse_args(["raw", "aa,55,43"])
    assert args.bytes == "aa,55,43"


def test_bridge_tool_device_default():
    import cli.bridge_tool as bt

    args = bt.build_parser().parse_args(["list"])
    assert args.device == "auto"


def test_bridge_tool_device_override():
    import cli.bridge_tool as bt

    args = bt.build_parser().parse_args(["--device", "/dev/hidraw1", "list"])
    assert args.device == "/dev/hidraw1"


def test_bridge_tool_unknown_command_exits_nonzero():
    import cli.bridge_tool as bt

    with pytest.raises(SystemExit) as exc:
        bt.build_parser().parse_args(["notacommand"])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# cli/maintenance.py
# ---------------------------------------------------------------------------


def test_maintenance_help_exits_zero():
    import sys
    import cli.maintenance as mt

    with pytest.raises(SystemExit) as exc:
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(sys, "argv", ["maintenance", "--help"])
            mt.build_parser().parse_args(["--help"])
    assert exc.value.code == 0


def test_maintenance_cleanup_discovery_default_args():
    import cli.maintenance as mt

    args = mt.build_parser().parse_args(["cleanup-discovery"])
    assert args.command == "cleanup-discovery"
    assert args.apply is False
    assert args.listen_seconds == 5


def test_maintenance_cleanup_discovery_apply_flag():
    import cli.maintenance as mt

    args = mt.build_parser().parse_args(["cleanup-discovery", "--apply"])
    assert args.apply is True


def test_maintenance_cleanup_discovery_listen_seconds():
    import cli.maintenance as mt

    args = mt.build_parser().parse_args(["cleanup-discovery", "--listen-seconds", "15"])
    assert args.listen_seconds == 15


def test_maintenance_no_command_exits_nonzero():
    import cli.maintenance as mt

    with pytest.raises(SystemExit) as exc:
        mt.build_parser().parse_args([])
    assert exc.value.code != 0


def test_maintenance_unknown_command_exits_nonzero():
    import cli.maintenance as mt

    with pytest.raises(SystemExit) as exc:
        mt.build_parser().parse_args(["not-a-command"])
    assert exc.value.code != 0
