"""
Sensor registry for WyzeSense2MQTT.

Owns all in-process and on-disk state for the known sensor fleet:
  - sensors.yaml  – user-editable per-sensor configuration (name, type, etc.)
  - state.yaml    – runtime-only state (last_seen, online) persisted across
                    restarts to smooth over brief outages

The SensorRegistry is the single source of truth for sensor data within the
bridge.  Nothing else should read or write sensors.yaml / state.yaml directly.
"""

import logging
import os
import time

from config import (
    SENSOR_STATE_FILE,
    SENSORS_CONFIG_FILE,
    config_path,
    read_yaml,
    write_yaml,
)

# ---------------------------------------------------------------------------
# Sensor type definitions
#
# Maps sensor_type string → metadata used for HA discovery and availability.
#
# Required keys per entry:
#   model         – human-readable model name shown in HA device info
#   hw_version    – hardware generation string ("V1" or "V2")
#   timeout_hours – hours of silence before a sensor is considered offline
#
# Binary sensor entries additionally require:
#   device_class  – HA binary_sensor device class (e.g. "opening", "motion")
#   state_on      – payload string meaning ON/triggered
#   state_off     – payload string meaning OFF/clear
#
# Climate sensors emit numeric entities only (temperature + humidity) and
# have no binary state fields.
# ---------------------------------------------------------------------------

SENSOR_TYPES: dict[str, dict] = {
    "motion": {
        "model": "Wyze Sense V1 Motion Sensor",
        "hw_version": "V1",
        "timeout_hours": 8,
        "device_class": "motion",
        "state_on": "active",
        "state_off": "inactive",
    },
    "motionv2": {
        "model": "Wyze Sense V2 Motion Sensor",
        "hw_version": "V2",
        "timeout_hours": 4,
        "device_class": "motion",
        "state_on": "active",
        "state_off": "inactive",
    },
    "switch": {
        "model": "Wyze Sense V1 Contact Sensor",
        "hw_version": "V1",
        "timeout_hours": 8,
        "device_class": "opening",
        "state_on": "open",
        "state_off": "closed",
    },
    "switchv2": {
        "model": "Wyze Sense V2 Contact Sensor",
        "hw_version": "V2",
        "timeout_hours": 4,
        "device_class": "opening",
        "state_on": "open",
        "state_off": "closed",
    },
    "leak": {
        "model": "Wyze Sense V2 Leak Sensor",
        "hw_version": "V2",
        "timeout_hours": 4,
        "device_class": "moisture",
        "state_on": "wet",
        "state_off": "dry",
    },
    "climate": {
        "model": "Wyze Sense V2 Climate Sensor",
        "hw_version": "V2",
        "timeout_hours": 4,
        # No binary state fields – climate sensors produce numeric entities only
    },
    "unknown": {
        "model": "WyzeSense Sensor",
        "hw_version": "unknown",
        "timeout_hours": 8,
    },
}

# Sensor types whose primary HA entity is a binary_sensor
BINARY_SENSOR_TYPES: frozenset[str] = frozenset(st for st, meta in SENSOR_TYPES.items() if "device_class" in meta)

# How far back to look for "fresh" state data on startup (seconds).
# State older than this is discarded to avoid showing stale availability.
STALE_STATE_SECONDS = 1 * 60 * 60  # 1 hour


def _default_state_entry() -> dict:
    return {"last_seen": time.time(), "online": True}


class SensorRegistry:
    """Manages sensor configuration and runtime state.

    Attributes:
        sensors:  dict[mac, config_dict]   – persisted to sensors.yaml
        state:    dict[mac, state_dict]    – persisted to state.yaml
    """

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger.getChild("sensors") if logger else logging.getLogger("ws2m.sensors")
        self.sensors: dict[str, dict] = {}
        self.state: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log(self, level: str, msg: str) -> None:
        """Convenience wrapper to call self._logger.<level>(msg)."""
        getattr(self._logger, level)(msg)

    # ------------------------------------------------------------------
    # Sensor config persistence
    # ------------------------------------------------------------------

    def load_sensors(self) -> bool:
        """Load sensors.yaml into self.sensors. Returns True if file existed."""
        path = config_path(SENSORS_CONFIG_FILE)
        if not os.path.isfile(path):
            self._log("warning", "No sensors config file found")
            self.sensors = {}
            return False

        data = read_yaml(path, self._logger) or {}
        self.sensors = data
        self._log("info", f"Loaded {len(self.sensors)} sensor(s) from sensors.yaml")

        # Back-fill any missing defaults
        for mac in self.sensors:
            self.sensors[mac].setdefault("invert_state", False)

        return True

    def save_sensors(self) -> bool:
        """Persist self.sensors to sensors.yaml."""
        ok = write_yaml(config_path(SENSORS_CONFIG_FILE), self.sensors, self._logger)
        if ok:
            self._log("debug", "Saved sensors.yaml")
        return ok

    def add_sensor(self, mac: str, sensor_type: str | None = None, sw_version: str | None = None) -> None:
        """Add a new sensor entry (or overwrite if already present).

        Initialises runtime state for the sensor as well.
        """
        self._log("info", f"Adding sensor to registry: {mac}")
        entry: dict = {
            "name": f"WyzeSense {mac}",
            "invert_state": False,
        }

        if sensor_type:
            entry["sensor_type"] = sensor_type
            type_meta = SENSOR_TYPES.get(sensor_type, {})
            if "device_class" in type_meta:
                entry["class"] = type_meta["device_class"]

        if sw_version:
            entry["sw_version"] = sw_version

        self.sensors[mac] = entry
        # Always initialise runtime state for new sensors
        self.state.setdefault(mac, _default_state_entry())
        self.save_sensors()

    def delete_sensor(self, mac: str) -> bool:
        """Remove a sensor from the registry and its runtime state."""
        self._log("info", f"Removing sensor from registry: {mac}")
        removed = False
        if mac in self.sensors:
            del self.sensors[mac]
            removed = True
            self.save_sensors()
        if mac in self.state:
            del self.state[mac]
        if not removed:
            self._log("error", f"Sensor {mac} not found in registry")
        return removed

    def update_sensor_type(self, mac: str, sensor_type: str) -> bool:
        """Update the sensor_type for an existing sensor. Returns True if changed."""
        if mac not in self.sensors:
            return False
        if self.sensors[mac].get("sensor_type") == sensor_type:
            return False
        self._log("info", f"Updating sensor type for {mac}: {sensor_type}")
        self.sensors[mac]["sensor_type"] = sensor_type
        type_meta = SENSOR_TYPES.get(sensor_type, {})
        if "device_class" in type_meta:
            self.sensors[mac]["class"] = type_meta["device_class"]
        self.save_sensors()
        return True

    # ------------------------------------------------------------------
    # Runtime state persistence
    # ------------------------------------------------------------------

    def load_state(self) -> bool:
        """Load state.yaml into self.state. Returns True if file existed."""
        path = config_path(SENSOR_STATE_FILE)
        if not os.path.isfile(path):
            self.state = {}
            return False

        raw = read_yaml(path, self._logger) or {}

        # Drop stale state
        modified = raw.pop("modified", None)
        if modified is not None and (time.time() - modified) > STALE_STATE_SECONDS:
            self._log("warning", "Discarding stale sensor state (older than 1 hour)")
            self.state = {}
            return True

        self.state = raw
        self._log("info", f"Loaded state for {len(self.state)} sensor(s) from state.yaml")
        return True

    def save_state(self) -> bool:
        """Persist self.state to state.yaml, with a 'modified' timestamp."""
        data = dict(self.state)
        data["modified"] = time.time()
        ok = write_yaml(config_path(SENSOR_STATE_FILE), data, self._logger)
        if ok:
            self._log("debug", "Saved state.yaml")
        return ok

    def ensure_state_entry(self, mac: str) -> None:
        """Initialise a state entry for *mac* if one does not already exist."""
        if mac not in self.state:
            self.state[mac] = _default_state_entry()

    def prune_state_to(self, linked_macs: list[str]) -> None:
        """Remove state entries for sensors not in *linked_macs*.

        Called after a successful dongle.list() so state entries for sensors
        no longer paired with the dongle are removed.
        """
        stale = [mac for mac in self.state if mac not in linked_macs]
        for mac in stale:
            del self.state[mac]
            self._log("warning", f"Pruned stale state entry for unlinked sensor {mac}")

    # ------------------------------------------------------------------
    # MAC validation
    # ------------------------------------------------------------------

    # Null-byte MACs seen from corrupt/uninitialized dongle state; all three
    # representations can appear depending on how the raw bytes were decoded.
    _INVALID_MACS: frozenset[str] = frozenset(["00000000", "\0\0\0\0\0\0\0\0", "\x00\x00\x00\x00\x00\x00\x00\x00"])

    @classmethod
    def is_valid_mac(cls, mac: str) -> bool:
        """Return True if *mac* looks like a real WyzeSense MAC address."""
        return len(str(mac)) == 8 and mac not in cls._INVALID_MACS

    # ------------------------------------------------------------------
    # Sensor type metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_type_meta(sensor_type: str) -> dict:
        """Return the SENSOR_TYPES entry for *sensor_type*, falling back to 'unknown'."""
        return SENSOR_TYPES.get(sensor_type, SENSOR_TYPES["unknown"])

    @staticmethod
    def timeout_for(sensor: dict) -> float:
        """Return the availability timeout in seconds for a sensor config dict.

        Checks for a per-sensor override ('timeout' key, in **seconds**) first,
        then falls back to the sensor-type default (stored as hours in SENSOR_TYPES).
        The per-sensor key has always been in seconds; no migration is needed.
        """
        sensor_type = sensor.get("sensor_type", "unknown")
        type_meta = SENSOR_TYPES.get(sensor_type, SENSOR_TYPES["unknown"])
        default_seconds = type_meta["timeout_hours"] * 3600
        return sensor.get("timeout", default_seconds)

    # ------------------------------------------------------------------
    # Reconciliation helpers (called during init_sensors)
    # ------------------------------------------------------------------

    def reconcile_with_dongle(self, linked_macs: list[str]) -> list[str]:
        """Ensure every MAC the dongle knows about has a sensor + state entry.

        Returns a list of MACs that were auto-added (not previously configured).
        """
        auto_added = []
        for mac in linked_macs:
            if not self.is_valid_mac(mac):
                continue
            if mac not in self.sensors:
                self.add_sensor(mac)
                auto_added.append(mac)
            else:
                self.ensure_state_entry(mac)

        self.prune_state_to(linked_macs)
        return auto_added

    def ensure_all_have_state(self) -> None:
        """Give a state entry to every configured sensor that lacks one.

        Called when dongle.list() failed; ensures every configured sensor has
        a state entry so availability checks can run even without a current
        paired-sensor list from the dongle.
        """
        for mac in self.sensors:
            self.ensure_state_entry(mac)
