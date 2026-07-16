"""
Sensor registry for WyzeSense2MQTT.

Owns all in-process and on-disk state for the known sensor fleet:
  - sensors.yaml  – user-editable per-sensor configuration, keyed by sensor MAC
  - state.yaml    – runtime-only state (last_seen, online, owning dongle)

There is exactly ONE registry for the whole hub, shared by all DongleWorkers.
Sensor configuration is a property of the sensor itself — its name, device
class, PINs, and so on follow the sensor if it is re-paired to a different
dongle.  The only dongle-specific fact is which dongle currently relays the
sensor, recorded as the ``dongle`` key in each state entry ("ownership").
Ownership is claimed at dongle init for unowned sensors and transfers
automatically when an event for a known sensor arrives via a different dongle.

All mutating operations and file writes are serialised through an internal
lock, and files are written atomically (temp file + os.replace), so concurrent
updates from multiple dongle workers can never corrupt the YAML files.

The SensorRegistry is the single source of truth for sensor data within the
bridge.  Nothing else should read or write these files directly.
"""

import logging
import os
import threading
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
#   device_class  – default HA binary_sensor device class
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
        # CR2450 3V Li coin — AON_BATMON raw / 32.0 = volts; usable range 2.4–3.2 V
        "battery_v_min": 2.4,
        "battery_v_max": 3.2,
    },
    "motionv2": {
        "model": "Wyze Sense V2 Motion Sensor",
        "hw_version": "V2",
        "timeout_hours": 4,
        "device_class": "motion",
        "state_on": "active",
        "state_off": "inactive",
        # CR2450 3V Li coin
        "battery_v_min": 2.4,
        "battery_v_max": 3.2,
    },
    "switch": {
        "model": "Wyze Sense V1 Contact Sensor",
        "hw_version": "V1",
        "timeout_hours": 8,
        "device_class": "opening",
        "state_on": "open",
        "state_off": "closed",
        # CR1632 3V Li coin — same chemistry, same voltage range as CR2450
        "battery_v_min": 2.4,
        "battery_v_max": 3.2,
    },
    "switchv2": {
        "model": "Wyze Sense V2 Contact Sensor",
        "hw_version": "V2",
        "timeout_hours": 4,
        "device_class": "opening",
        "state_on": "open",
        "state_off": "closed",
        # 1× AAA alkaline 1.5 V; AON_BATMON reports at half scale so raw is doubled before /32.
        # Actual cell voltage range: 0.9–1.6 V; post-doubling: 1.8–3.2 V (same scale as 3 V coins).
        "battery_v_min": 1.8,
        "battery_v_max": 3.2,
        "battery_raw_double": True,
    },
    "leak": {
        "model": "Wyze Sense V2 Leak Sensor",
        "hw_version": "V2",
        "timeout_hours": 4,
        "device_class": "moisture",
        "state_on": "wet",
        "state_off": "dry",
        # invert_state not applicable — moisture/wet/dry already correct semantics
        # CR2450 3V Li coin
        "battery_v_min": 2.4,
        "battery_v_max": 3.2,
    },
    "climate": {
        "model": "Wyze Sense V2 Climate Sensor",
        "hw_version": "V2",
        "timeout_hours": 4,
        # No binary state fields — numeric entities only
        # CR2450 3V Li coin
        "battery_v_min": 2.4,
        "battery_v_max": 3.2,
    },
    "chime": {
        "model": "Wyze Sense Chime",
        "hw_version": "V1",
        "timeout_hours": 24,
        # Output-only RF speaker; per-device config in sensors.yaml: ring_id, repeat_count, volume
        # Battery chemistry unknown; voltage published only, no percentage estimate.
    },
    "keypad": {
        "model": "Wyze Sense V2 Keypad",
        "hw_version": "V2",
        "timeout_hours": 4,
        # Per-device config in sensors.yaml: pins (list[str]) — valid PIN codes
        # Keypad battery uses a 0–155 raw scale (not AON_BATMON); handled separately.
        "battery_keypad_scale": True,
    },
    "unknown": {
        "model": "WyzeSense Sensor",
        "hw_version": "unknown",
        "timeout_hours": 8,
    },
}

# Sensor types whose primary HA entity is a binary_sensor
BINARY_SENSOR_TYPES: frozenset[str] = frozenset(st for st, meta in SENSOR_TYPES.items() if "device_class" in meta)

# Sensor types that support invert_state (leak excluded — moisture/wet/dry semantics are already correct)
INVERTIBLE_SENSOR_TYPES: frozenset[str] = frozenset(["motion", "motionv2", "switch", "switchv2"])

# Valid HA device_class values selectable per sensor family.
# Used to populate the device_class select entity in HA.
DEVICE_CLASS_OPTIONS: dict[str, list[str]] = {
    # Contact sensors — opening is the default; door/window are common alternates
    "switch": ["door", "garage_door", "lock", "opening", "window"],
    "switchv2": ["door", "garage_door", "lock", "opening", "window"],
    # Motion sensors — motion is the default; occupancy is a common alternate
    "motion": ["motion", "occupancy"],
    "motionv2": ["motion", "occupancy"],
}


# How far back to look for "fresh" state data on startup (seconds).
# State older than this is discarded to avoid showing stale availability.
def _default_state_entry() -> dict:
    return {"last_seen": time.time(), "online": True}


class SensorRegistry:
    """Manages sensor configuration and runtime state for the whole hub.

    One instance is shared by all DongleWorkers; data lives in
    <CONFIG_DIR>/sensors.yaml and <CONFIG_DIR>/state.yaml.

    Attributes:
        sensors:  dict[mac, config_dict]  – persisted to sensors.yaml
        state:    dict[mac, state_dict]   – persisted to state.yaml; each
                  entry may carry a ``dongle`` key naming the owning dongle

    Thread safety: all mutating methods and saves acquire an internal RLock;
    files are written atomically so a crash mid-write leaves the previous
    file intact.  Direct dict-item mutation of an existing sensor entry
    followed by save_sensors() is also safe — the save serialises a snapshot
    under the lock.
    """

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger.getChild("sensors") if logger else logging.getLogger("ws2m.sensors")
        self._lock = threading.RLock()
        self.sensors: dict[str, dict] = {}
        self.state: dict[str, dict] = {}

    def _data_path(self, filename: str) -> str:
        """Return the full path to a registry data file."""
        return config_path(filename)

    # ------------------------------------------------------------------
    # Dongle ownership
    # ------------------------------------------------------------------

    def owner_of(self, mac: str) -> str | None:
        """Return the MAC of the dongle that owns *mac*, or None if unowned."""
        return self.state.get(mac, {}).get("dongle")

    def set_owner(self, mac: str, dongle_mac: str) -> None:
        """Record *dongle_mac* as the owning dongle for sensor *mac*.

        Clears any orphan staleness marker — the sensor is demonstrably alive.
        """
        with self._lock:
            entry = self.state.setdefault(mac, _default_state_entry())
            entry["dongle"] = dongle_mac
            entry.pop("stale_since", None)

    def mark_stale(self, mac: str, now: float | None = None) -> None:
        """Record when *mac* was first seen orphaned (idempotent)."""
        with self._lock:
            if mac in self.state:
                self.state[mac].setdefault("stale_since", now if now is not None else time.time())

    def clear_stale(self, mac: str) -> None:
        """Remove the orphan staleness marker for *mac*."""
        with self._lock:
            if mac in self.state:
                self.state[mac].pop("stale_since", None)

    def stale_macs(self, older_than_seconds: float) -> list[str]:
        """Return MACs whose stale_since marker is older than *older_than_seconds*."""
        cutoff = time.time() - older_than_seconds
        with self._lock:
            return [
                mac
                for mac, entry in self.state.items()
                if entry.get("stale_since") is not None and entry["stale_since"] < cutoff
            ]

    def macs_owned_by(self, dongle_mac: str) -> list[str]:
        """Return all sensor MACs currently owned by *dongle_mac*."""
        with self._lock:
            return [mac for mac, entry in self.state.items() if entry.get("dongle") == dongle_mac]

    def known_dongle_macs(self) -> set[str]:
        """Return the set of dongle MACs that own at least one sensor."""
        with self._lock:
            return {entry["dongle"] for entry in self.state.values() if entry.get("dongle")}

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
        path = self._data_path(SENSORS_CONFIG_FILE)
        if not os.path.isfile(path):
            self._log("warning", "No sensors config file found")
            with self._lock:
                self.sensors = {}
            return False

        data = read_yaml(path, self._logger) or {}
        with self._lock:
            self.sensors = data
        self._log("info", f"Loaded {len(self.sensors)} sensor(s) from {path}")

        # Back-fill defaults and migrate legacy fields
        for mac in self.sensors:
            entry = self.sensors[mac]
            entry.setdefault("invert_state", False)
            # Remove legacy 'timeout' override — availability timeouts are now
            # type-driven constants in SENSOR_TYPES and are not user-configurable.
            entry.pop("timeout", None)

        return True

    def save_sensors(self) -> bool:
        """Persist self.sensors to sensors.yaml (atomic, serialised)."""
        with self._lock:
            ok = write_yaml(self._data_path(SENSORS_CONFIG_FILE), self.sensors, self._logger)
        if ok:
            self._log("debug", "Saved sensors.yaml")
        return ok

    def add_sensor(
        self,
        mac: str,
        sensor_type: str | None = None,
        sw_version: str | None = None,
        dongle_mac: str | None = None,
    ) -> None:
        """Add a new sensor entry (or overwrite if already present).

        Initialises runtime state for the sensor as well; if *dongle_mac* is
        given it is recorded as the owning dongle.
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

        with self._lock:
            self.sensors[mac] = entry
            # Always initialise runtime state for new sensors
            self.state.setdefault(mac, _default_state_entry())
            if dongle_mac:
                self.state[mac]["dongle"] = dongle_mac
            self.save_sensors()

    def delete_sensor(self, mac: str) -> bool:
        """Remove a sensor from the registry and its runtime state."""
        self._log("info", f"Removing sensor from registry: {mac}")
        removed = False
        with self._lock:
            if mac in self.sensors:
                del self.sensors[mac]
                removed = True
                self.save_sensors()
            if mac in self.state:
                del self.state[mac]
                self.save_state()
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
            # Only reset class to type default if the user hasn't overridden it
            # via the HA device_class select entity (i.e. class == old type default).
            self.sensors[mac]["class"] = type_meta["device_class"]
        self.save_sensors()
        return True

    # ------------------------------------------------------------------
    # Runtime state persistence
    # ------------------------------------------------------------------

    def load_state(self) -> bool:
        """Load state.yaml into self.state. Returns True if file existed."""
        path = self._data_path(SENSOR_STATE_FILE)
        if not os.path.isfile(path):
            self.state = {}
            return False

        raw = read_yaml(path, self._logger) or {}

        # Load state as-is, however old.  last_seen values are absolute
        # timestamps, so after any downtime the per-sensor-type availability
        # timeouts (4 h / 8 h / 24 h) naturally mark timed-out sensors offline
        # on the first availability sweep — no separate staleness cutoff is
        # needed, and dongle ownership survives arbitrary downtime.
        raw.pop("modified", None)
        with self._lock:
            self.state = raw
        self._log("info", f"Loaded state for {len(self.state)} sensor(s) from {path}")
        return True

    def save_state(self) -> bool:
        """Persist self.state to state.yaml, with a 'modified' timestamp (atomic, serialised)."""
        with self._lock:
            data = dict(self.state)
            data["modified"] = time.time()
            ok = write_yaml(self._data_path(SENSOR_STATE_FILE), data, self._logger)
        if ok:
            self._log("debug", "Saved state.yaml")
        return ok

    def ensure_state_entry(self, mac: str, dongle_mac: str | None = None) -> None:
        """Initialise a state entry for *mac* if one does not already exist.

        If *dongle_mac* is given and the entry has no owner yet, it is claimed.
        """
        with self._lock:
            if mac not in self.state:
                self.state[mac] = _default_state_entry()
            if dongle_mac and not self.state[mac].get("dongle"):
                self.state[mac]["dongle"] = dongle_mac

    def prune_state_for_dongle(self, dongle_mac: str, linked_macs: list[str]) -> None:
        """Remove state entries owned by *dongle_mac* for sensors not in *linked_macs*.

        Called after a successful dongle.list() so state entries for sensors
        no longer paired with that dongle are removed.  Entries owned by other
        dongles (or unowned) are never touched.
        """
        with self._lock:
            stale = [
                mac for mac, entry in self.state.items() if entry.get("dongle") == dongle_mac and mac not in linked_macs
            ]
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

        The timeout is determined entirely by sensor type — there is no per-sensor
        override.  Type defaults are defined in SENSOR_TYPES['timeout_hours'].
        """
        sensor_type = sensor.get("sensor_type", "unknown")
        type_meta = SENSOR_TYPES.get(sensor_type, SENSOR_TYPES["unknown"])
        return type_meta["timeout_hours"] * 3600

    # ------------------------------------------------------------------
    # Keypad PIN management
    # ------------------------------------------------------------------

    def add_pin(self, mac: str, pin: str) -> bool:
        """Add a PIN to the sensor's configured pins list.

        Returns True if the PIN was added (False if already present or sensor unknown).
        """
        if mac not in self.sensors:
            return False
        pins = self.sensors[mac].get("pins", [])
        if isinstance(pins, str):
            pins = [pins]
        if pin in pins:
            return False
        pins.append(pin)
        self.sensors[mac]["pins"] = pins
        self.save_sensors()
        self._log("info", f"Added PIN to {mac} (total: {len(pins)})")
        return True

    def clear_pins(self, mac: str) -> bool:
        """Remove all PINs from the sensor's configured pins list.

        Returns True if any PINs were cleared.
        """
        if mac not in self.sensors:
            return False
        pins = self.sensors[mac].get("pins", [])
        if isinstance(pins, str):
            pins = [pins]
        if not pins:
            return False
        self.sensors[mac]["pins"] = []
        self.save_sensors()
        self._log("info", f"Cleared all PINs from {mac}")
        return True

    def pin_count(self, mac: str) -> int:
        """Return the number of configured PINs for a sensor."""
        if mac not in self.sensors:
            return 0
        pins = self.sensors[mac].get("pins", [])
        if isinstance(pins, str):
            return 1 if pins else 0
        return len(pins)

    # ------------------------------------------------------------------
    # Reconciliation helpers (called during init_sensors)
    # ------------------------------------------------------------------

    def reconcile_with_dongle(self, dongle_mac: str, linked_macs: list[str]) -> list[str]:
        """Reconcile *dongle_mac*'s paired-sensor list with the registry.

        For each valid MAC in *linked_macs*:
          - unknown sensor → auto-added with *dongle_mac* as owner
          - known but unowned (e.g. migrated 3.1.0 state) → claimed
          - known and owned by another dongle → left alone (stale NVRAM pairing;
            ownership only transfers when events actually arrive via this
            dongle), logged so the user can clean up the old pairing

        State entries owned by this dongle but absent from *linked_macs* are
        pruned.  Returns a list of MACs that were auto-added.
        """
        auto_added = []
        with self._lock:
            for mac in linked_macs:
                if not self.is_valid_mac(mac):
                    continue
                if mac not in self.sensors:
                    self.add_sensor(mac, dongle_mac=dongle_mac)
                    auto_added.append(mac)
                    continue
                owner = self.owner_of(mac)
                if owner is None:
                    self.ensure_state_entry(mac, dongle_mac)
                    self.set_owner(mac, dongle_mac)
                elif owner == dongle_mac:
                    self.ensure_state_entry(mac, dongle_mac)
                    self.clear_stale(mac)
                else:
                    self._log(
                        "info",
                        f"Sensor {mac} is paired in dongle {dongle_mac}'s NVRAM but currently "
                        f"owned by dongle {owner} — ownership transfers on first event via "
                        f"{dongle_mac}; remove the stale pairing from {dongle_mac} if unintended",
                    )

            self.prune_state_for_dongle(dongle_mac, linked_macs)
        return auto_added

    def ensure_owned_have_state(self, dongle_mac: str) -> None:
        """Give a state entry to every sensor owned by *dongle_mac* that lacks one.

        Called when dongle.list() failed; ensures the dongle's sensors keep
        state entries so availability checks can run even without a current
        paired-sensor list.  (State entries normally imply ownership, so this
        mainly re-creates entries lost to the stale-state discard on load.)
        """
        with self._lock:
            for mac in self.sensors:
                if self.owner_of(mac) == dongle_mac:
                    self.ensure_state_entry(mac, dongle_mac)
