"""
Hardware smoke tests for the WyzeSense USB dongle.

These tests require a physical dongle attached to the test machine and are
skipped by default.  Run them explicitly with:

    pytest -m dongle                       (auto-detects the dongle device)
    pytest -m dongle --dongle /dev/hidraw0 (use a specific device path)
    pytest -m dongle --dongle auto         (explicitly request auto-detection)

'auto' (the default) exercises the same find_dongle_device() code path
that the bridge uses at startup.

The bridge service MUST NOT be running while these tests execute — both
cannot hold the dongle open simultaneously.

Scope
-----
These tests cover only the initialisation handshake and basic protocol
queries.  They do NOT pair or unpair sensors, so they are safe to run
against a dongle with or without sensors already paired.

What is tested:
  - Dongle opens and completes the full init sequence (inquiry → ENR → MAC
    → version → finish_auth)
  - MAC address is 8 characters, printable ASCII, non-null
  - Version string is non-empty
  - ENR is 16 bytes
  - list() completes without timeout and returns a list (may be empty)
  - Worker thread remains alive after a short idle period
  - check_error() does not raise after idle
"""

import time

import pytest

# ---------------------------------------------------------------------------
# pytest option and mark registration (also in conftest.py via addoption)
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    """Add --dongle option.  Registered here so the file is self-contained."""
    try:
        parser.addoption(
            "--dongle",
            default="auto",
            metavar="PATH|auto",
            help="Path to the WyzeSense USB HID device, or 'auto' to use the "
                 "same auto-detection as the bridge (default: auto)",
        )
    except ValueError:
        # Already registered by conftest.py — ignore
        pass


# ---------------------------------------------------------------------------
# Fixture: open the dongle once per test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def dongle(request):
    """Open the dongle and yield it; stop on teardown."""
    import sys
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wyzesense2mqtt"))
    import dongle_protocol as dp

    device = request.config.getoption("--dongle", default="auto")
    events = []

    if device.lower() == "auto":
        # Exercise the same auto-detection path the bridge uses at startup
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wyzesense2mqtt"))
        from config import find_dongle_device
        detected = find_dongle_device()
        if detected is None:
            pytest.skip("Auto-detection found no WyzeSense dongle in /sys/class/hidraw")
        device = detected

    def _on_event(dongle, event):
        events.append(event)

    try:
        d = dp.open_dongle(device, _on_event)
    except OSError as err:
        pytest.skip(f"Could not open dongle at {device}: {err}")

    d._test_events = events
    yield d
    d.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.dongle
def test_dongle_device_resolved(dongle, request):
    """When --dongle auto is used, a real device path should have been found."""
    opt = request.config.getoption("--dongle", default="auto")
    if opt.lower() == "auto":
        # If we got this far (dongle fixture didn't skip), auto-detection worked
        assert dongle._thread.is_alive(), "Auto-detected dongle did not initialise"


@pytest.mark.dongle
def test_dongle_mac_is_valid(dongle):
    """Dongle MAC should be 8 printable ASCII characters, non-null."""
    from sensors import SensorRegistry

    mac = dongle.mac
    assert isinstance(mac, str), f"MAC is not a string: {mac!r}"
    assert len(mac) == 8, f"MAC is not 8 characters: {mac!r}"
    assert mac.isascii(), f"MAC contains non-ASCII characters: {mac!r}"
    assert mac != "00000000", "MAC is all zeros (dongle did not initialise correctly)"
    assert SensorRegistry.is_valid_mac(mac), f"MAC failed is_valid_mac check: {mac!r}"


@pytest.mark.dongle
def test_dongle_version_is_non_empty(dongle):
    """Dongle firmware version string should be non-empty."""
    version = dongle.version
    assert isinstance(version, str), f"Version is not a string: {version!r}"
    assert len(version) > 0, "Version string is empty"


@pytest.mark.dongle
def test_dongle_enr_is_16_bytes(dongle):
    """ENR (encryption nonce) should be exactly 16 bytes."""
    enr = dongle.enr
    assert isinstance(enr, bytes), f"ENR is not bytes: {enr!r}"
    assert len(enr) == 16, f"ENR is {len(enr)} bytes, expected 16"


@pytest.mark.dongle
def test_dongle_worker_thread_alive(dongle):
    """The background worker thread should still be running after init."""
    assert dongle._thread.is_alive(), "Dongle worker thread has died"


@pytest.mark.dongle
def test_dongle_no_error_after_idle(dongle):
    """check_error() should not raise after a short idle period."""
    time.sleep(2)
    try:
        dongle.check_error()
    except Exception as err:
        pytest.fail(f"check_error() raised after idle: {err}")


@pytest.mark.dongle
def test_dongle_list_returns_list(dongle):
    """list() should complete without timeout and return a list."""
    try:
        sensors = dongle.list()
    except TimeoutError:
        pytest.fail("dongle.list() timed out")

    assert isinstance(sensors, list), f"list() returned {type(sensors)}, expected list"


@pytest.mark.dongle
def test_dongle_list_macs_are_valid(dongle):
    """Any MAC addresses returned by list() should pass is_valid_mac."""
    from sensors import SensorRegistry

    try:
        sensors = dongle.list()
    except TimeoutError:
        pytest.skip("dongle.list() timed out — skipping MAC validation")

    for mac in sensors:
        assert SensorRegistry.is_valid_mac(mac), (
            f"Dongle returned invalid MAC {mac!r} — "
            "consider running 'python3 -m cli.bridge_tool fix' to remove corrupt entries"
        )


@pytest.mark.dongle
def test_dongle_worker_still_alive_after_list(dongle):
    """Worker thread should remain alive after a list() call."""
    assert dongle._thread.is_alive(), "Dongle worker thread died after list()"
