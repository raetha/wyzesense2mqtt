"""
Bridge orchestration for WyzeSense2MQTT.

Architecture
------------
Bridge        — top-level orchestrator: config, MQTT connection, service discovery,
                service-level MQTT commands (reload, log_level), DongleWorker lifecycle.
DongleWorker  — owns one physical dongle + its SensorRegistry; handles all
                sensor events and dongle-scoped MQTT commands (scan, remove, sensor config).

Each DongleWorker runs independently against the shared MqttGateway.  When
dongle is "auto", all detected dongles get a worker; when it is an explicit
path, exactly one worker is created for that device.

Startup sequence
----------------
  Bridge.start()
    ├─ _load_config()
    ├─ _load_hub_identity()  (integrated in start())
    ├─ _connect_mqtt()
    ├─ publish_hub_discovery()
    └─ for each dongle device:
         DongleWorker.start()
           ├─ _open_dongle()
           ├─ _migrate_legacy_sensor_files()   (flat → per-dongle dir, once)
           ├─ _publish_dongle_discovery()
           └─ _init_sensors()

MQTT topics (v2 schema)
-----------------------
  <self_topic_root>/hub/<uuid>/reload                     — hub reload command
  <self_topic_root>/hub/<uuid>/log_level                  — hub log level state (retained)
  <self_topic_root>/hub/<uuid>/log_level/set              — hub log level command
  <self_topic_root>/hub/<uuid>/cleanup_removed_dongles    — hub cleanup command
  <self_topic_root>/hub/<uuid>/remote_pair                       — enable remote pairing mode command
  <self_topic_root>/hub/<uuid>/remote_pairing               — pairing mode state (retained)
  <self_topic_root>/hub/<uuid>/dongle                   — USB dongle config state (retained)
  <self_topic_root>/hub/<uuid>/dongle/set               — USB dongle config command
  <self_topic_root>/hub/<uuid>/ws_port                      — WebSocket port state (retained)
  <self_topic_root>/hub/<uuid>/ws_port/set                  — WebSocket port command
  <self_topic_root>/hub/<uuid>/remote_pairing_timeout       — Remote pairing timeout state (retained)
  <self_topic_root>/hub/<uuid>/remote_pairing_timeout/set   — Remote pairing timeout command
  <self_topic_root>/hub/<uuid>/mdns                         — mDNS advertisement state (retained)
  <self_topic_root>/hub/<uuid>/mdns/set                     — mDNS advertisement command
  <self_topic_root>/hub/<uuid>/ws_enabled                    — WebSocket listener enabled state (retained)
  <self_topic_root>/hub/<uuid>/ws_enabled/set                — WebSocket listener enabled command
  <self_topic_root>/hub/<uuid>/restart/set                   — hub restart command
  <self_topic_root>/hub/<uuid>/health                     — hub health state (retained)
  <self_topic_root>/hub/<uuid>/connected_dongles          — count of working local dongles (retained)
  <self_topic_root>/hub/<uuid>/remote_dongles             — aggregate count of all remote-relayed dongles (retained)
  <self_topic_root>/hub/<uuid>/connected_remotes          — count of distinct connected remotes (retained)
  <self_topic_root>/dongle/<mac>/scan                     — scan for new sensor on this dongle
  <self_topic_root>/dongle/<mac>/remove                   — remove sensor by MAC payload (sensor's own Remove button)
  <self_topic_root>/dongle/<mac>/status                   — dongle online/offline (retained)
  <self_topic_root>/sensor/<sensor_mac>/status            — sensor online/offline (retained)
  <self_topic_root>/sensor/<sensor_mac>                   — sensor data payload
  <self_topic_root>/sensor/<sensor_mac>/sensor_name       — sensor display name state (retained)
  <self_topic_root>/sensor/<sensor_mac>/sensor_name/set   — sensor display name command
  <self_topic_root>/sensor/<sensor_mac>/device_class      — HA device class state (retained)
  <self_topic_root>/sensor/<sensor_mac>/device_class/set  — HA device class command
  <self_topic_root>/sensor/<sensor_mac>/invert_state      — invert state switch state (retained)
  <self_topic_root>/sensor/<sensor_mac>/invert_state/set  — invert state switch command
  <self_topic_root>/sensor/<sensor_mac>/set               — keypad command (alarm state in)
  <self_topic_root>/sensor/<sensor_mac>/pin               — keypad PIN confirm event
  <self_topic_root>/sensor/<sensor_mac>/alarm             — keypad alarm sub-event
  <self_topic_root>/sensor/<sensor_mac>/pin_count         — keypad PIN count state (retained)
  <self_topic_root>/sensor/<sensor_mac>/add_pin           — keypad: arm PIN capture mode
  <self_topic_root>/sensor/<sensor_mac>/clear_pins        — keypad: clear all PINs
  <self_topic_root>/sensor/<sensor_mac>/play              — chime play button
  <self_topic_root>/sensor/<sensor_mac>/<param>/set       — chime number set
  <self_topic_root>/remote/<uuid>/status                  — remote online/offline (retained)
  <self_topic_root>/remote/<uuid>/health                  — remote health state (retained)
  <self_topic_root>/remote/<uuid>/connected_dongles       — count of working remote dongles (retained)
  <self_topic_root>/remote/<uuid>/restart/set                      — remote restart command
  <self_topic_root>/remote/<uuid>/remove                           — remove remote and clear all its topics
  <self_topic_root>/remote/<uuid>/cleanup_disconnected_dongles     — clear topics/data for failed remote dongles
"""

import json
import logging
import pathlib
import shutil
import threading
import time
from collections import deque

import config as _cfg_mod
import dongle_protocol
from config import (
    VERSION,
    dongle_data_path,
    find_all_dongle_devices,
    init_logging,
    list_known_dongle_macs,
    load_config,
    load_hub_id,
    migrate_legacy_sensor_files,
    save_config,
)
from mqtt import (
    DISCOVERY_SCHEMA_VERSION,
    LOG_LEVEL_OPTIONS,
    QOS_COMMAND,
    QOS_NUMBER,
    QOS_STATUS,
    RETAIN_NUMBER,
    RETAIN_STATUS,
    MqttGateway,
)
from retrying import retry
from sensors import DEVICE_CLASS_OPTIONS, INVERTIBLE_SENSOR_TYPES, SensorRegistry
from sensors import SENSOR_TYPES as _SENSOR_TYPES

# ---------------------------------------------------------------------------
# Remote WebSocket transport
#
# RemoteTransport is a ws2m hub concern, not part of the standalone dongle_protocol
# library.  It wraps an authenticated WebSocket connection from a ws2m-remote process
# and presents the same Transport interface that Dongle uses for local HID fds.
# ---------------------------------------------------------------------------


class RemoteTransport(dongle_protocol.Transport):
    """Transport backed by a WebSocket connection from a ws2m-remote process.

    The remote sends raw HID reports as binary WebSocket messages (dongle→hub
    direction) and receives raw protocol bytes as binary WebSocket messages
    (hub→dongle direction).

    Replay frames collected during the auth handshake are delivered first via
    an in-memory deque before switching to live WebSocket reads.

    JSON text control frames are intercepted and routed to callbacks:
      - remote_unhealthy / remote_healthy → on_health_change(remote_id, bool)
      - set_dongle                        → on_set_dongle(remote_id, value)
      - set_log_level                     → on_set_log_level(remote_id, level)
      - (all others are logged and discarded)
    """

    _log = logging.getLogger("ws2m.transport")

    def __init__(
        self,
        ws_connection,
        remote_id: str,
        dongle_mac: str,
        replay_frames: list[bytes],
        on_health_change=None,
        on_set_dongle=None,
        on_set_log_level=None,
    ):
        self._ws = ws_connection
        self._remote_id = remote_id
        self._dongle_mac = dongle_mac
        self._replay: deque[bytes] = deque(replay_frames)
        self._on_health_change = on_health_change  # Callable[[str, bool], None] | None
        self._on_set_dongle = on_set_dongle  # Callable[[str, str], None] | None
        self._on_set_log_level = on_set_log_level  # Callable[[str, str], None] | None

    @property
    def remote_id(self) -> str:
        return self._remote_id

    @property
    def dongle_mac(self) -> str:
        return self._dongle_mac

    def read(self) -> bytes:
        if self._replay:
            return self._replay.popleft()
        msg = self._ws.recv()
        if isinstance(msg, str):
            self._handle_control(msg)
            return b""
        return bytes(msg)

    def _handle_control(self, msg: str) -> None:
        """Dispatch an incoming JSON text control frame."""
        try:
            parsed = json.loads(msg)
        except Exception:
            self._log.warning("RemoteTransport: non-JSON text from remote: %s", msg[:120])
            return

        msg_type = parsed.get("type", "")

        if msg_type == "remote_unhealthy":
            self._log.warning("RemoteTransport: remote reported unhealthy: %s", parsed.get("reason", ""))
            if self._on_health_change is not None:
                self._on_health_change(self._remote_id, False)

        elif msg_type == "remote_healthy":
            self._log.info("RemoteTransport: remote reported healthy")
            if self._on_health_change is not None:
                self._on_health_change(self._remote_id, True)

        else:
            self._log.warning("RemoteTransport: unexpected control message from remote: %s", msg[:120])

    def write(self, data: bytes) -> None:
        self._ws.send(data)

    # ------------------------------------------------------------------
    # Hub → remote control frames
    # ------------------------------------------------------------------

    def send_restart(self) -> None:
        """Send a restart command to the remote."""
        self._ws.send(json.dumps({"type": "restart"}))

    def send_dongle(self, value: str) -> None:
        """Send a dongle path update to the remote (effective after remote restart)."""
        self._ws.send(json.dumps({"type": "set_dongle", "value": value}))

    def send_log_level(self, level: str) -> None:
        """Send a log level change to the remote (applied immediately by the remote)."""
        self._ws.send(json.dumps({"type": "set_log_level", "level": level}))

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Health file
# ---------------------------------------------------------------------------

_HEALTH_FILE = pathlib.Path("/tmp/ws2m_healthy")  # noqa: S108


def _mark_healthy() -> None:
    _HEALTH_FILE.touch()


def _mark_unhealthy() -> None:
    _HEALTH_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# DongleWorker
# ---------------------------------------------------------------------------


class DongleWorker:
    """Manages one physical USB dongle and its associated sensor population.

    Owns:
      - dongle_protocol.Dongle  — USB HID communication
      - SensorRegistry          — per-dongle sensor config and state
      - MQTT subscriptions scoped to this dongle's topics

    The shared MqttGateway and config are passed in from the parent Bridge.
    """

    def __init__(
        self,
        device_path: str | None,
        gateway: MqttGateway,
        cfg: dict,
        hub_id: str,
        logger: logging.Logger,
        transport: dongle_protocol.Transport | None = None,
        remote_id: str | None = None,
        on_remote_health_change=None,
    ):
        self._device_path = device_path or "<remote>"
        self._transport = transport  # pre-built transport for remote dongles; None for local
        self._remote_id = remote_id  # UUID of the ws2m-remote process, if any
        self._on_remote_health_change = on_remote_health_change  # Callable[[str, bool], None] | None
        self._gateway = gateway
        self._config = cfg
        self._hub_id = hub_id
        self._logger = logger.getChild("worker")

        self._dongle: dongle_protocol.Dongle | None = None
        self._registry: SensorRegistry | None = None
        self._initialized = False
        self._failed = False
        self._stopped = False

        # Dongle-MAC-derived topics (set after dongle opens)
        self._scan_topic = ""
        self._remove_topic = ""
        self._dongle_status_topic = ""

        # Keypad command topics: sensor_mac → topic
        self._keypad_command_topics: dict[str, str] = {}
        # Keypad PIN capture state: MAC → True when armed for next PIN entry
        self._keypad_pin_capture: dict[str, bool] = {}
        # Chime command topics that are currently subscribed
        self._chime_subscribed: set[str] = set()
        # Sensor config command topics that are currently subscribed
        self._sensor_config_subscribed: set[str] = set()
        # MACs we've already warned about for auto-add
        self._auto_add_warned: set[str] = set()

    @property
    def dongle_mac(self) -> str | None:
        """Return the dongle's MAC address, or None if the dongle is not yet open."""
        return self._dongle.mac if self._dongle else None

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def failed(self) -> bool:
        """True if this worker's dongle has encountered a fatal hardware error."""
        return self._failed

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open dongle, load sensors, publish discovery, subscribe to topics."""
        self._open_dongle()

        mac = self._dongle.mac
        self._registry = SensorRegistry(mac, self._logger)

        # One-time migration of legacy flat files → per-dongle directory
        migrate_legacy_sensor_files(mac, self._logger)

        cfg = self._config
        self._scan_topic = f"{cfg['self_topic_root']}/dongle/{mac}/scan"
        self._remove_topic = f"{cfg['self_topic_root']}/dongle/{mac}/remove"
        self._dongle_status_topic = f"{cfg['self_topic_root']}/dongle/{mac}/status"

        # Publish dongle offline until fully initialised
        self._gateway.publish(self._dongle_status_topic, "offline", is_json=False, qos=QOS_STATUS, retain=RETAIN_STATUS)

        # Subscribe to dongle-scoped command topics
        self._subscribe_dongle_topics()

        if cfg["hass_discovery"]:
            self._gateway.publish_dongle_discovery(
                mac, self._dongle.version, self._hub_id, via_remote_id=self._remote_id
            )

        self._init_sensors()

        self._initialized = True
        self._logger.info(f"DongleWorker ready — dongle={mac}  sensors={len(self._registry.sensors)}")
        self._gateway.publish(self._dongle_status_topic, "online", is_json=False, qos=QOS_STATUS, retain=RETAIN_STATUS)

    @retry(
        wait_exponential_multiplier=1000,
        wait_exponential_max=30000,
        retry_on_exception=lambda e: isinstance(e, OSError),
    )
    def _open_dongle(self) -> None:
        if self._transport is not None:
            # Remote dongle via WebSocket — transport already authenticated by ws_listener.
            # Construct the Dongle directly from the existing RemoteTransport.
            remote_id = getattr(self._transport, "remote_id", "<unknown>")
            dongle_mac = getattr(self._transport, "dongle_mac", "<unknown>")
            self._logger.info(f"Opening remote dongle remote_id={remote_id!r} dongle_mac={dongle_mac!r}")
            dongle_protocol._logger = self._logger  # type: ignore[attr-defined]
            self._dongle = dongle_protocol.Dongle(self._transport, self._on_dongle_event)
        else:
            self._logger.info(f"Opening dongle at {self._device_path}")
            try:
                self._dongle = dongle_protocol.open_dongle(self._device_path, self._on_dongle_event, self._logger)
            except OSError as err:
                self._logger.warning(f"Could not open dongle at {self._device_path}: {err} — will retry")
                raise
        _kind = "remote" if self._transport is not None else "local"
        self._logger.info(f"Dongle ready ({_kind}) — MAC: {self._dongle.mac}  VER: {self._dongle.version}")

    def _subscribe_dongle_topics(self) -> None:
        """Subscribe to scan/remove command topics for this dongle."""
        client = self._gateway.client
        client.subscribe(
            [
                (self._scan_topic, QOS_COMMAND),
                (self._remove_topic, QOS_COMMAND),
            ]
        )
        client.message_callback_add(self._scan_topic, self._on_mqtt_scan)
        client.message_callback_add(self._remove_topic, self._on_mqtt_remove)
        self._logger.debug(f"Subscribed to {self._scan_topic} and {self._remove_topic}")

    def resubscribe(self) -> None:
        """Re-subscribe to all MQTT topics after a reconnect.

        Called by Bridge._on_connect when the broker connection is re-established.
        """
        if not self._initialized:
            return
        self._subscribe_dongle_topics()
        for mac, sensor in list(self._registry.sensors.items()):
            if sensor.get("sensor_type") == "keypad":
                self._subscribe_keypad_command(mac)
        for topic in list(self._chime_subscribed):
            self._gateway.client.subscribe(topic, QOS_COMMAND)
        for mac, sensor in list(self._registry.sensors.items()):
            if sensor.get("sensor_type") == "chime":
                self._subscribe_chime(mac)
        for topic in list(self._sensor_config_subscribed):
            self._gateway.client.subscribe(topic, QOS_COMMAND)
        for mac in list(self._registry.sensors.keys()):
            self._subscribe_sensor_config(mac)
        # Re-publish dongle status
        self._gateway.publish(
            self._dongle_status_topic, "online", is_json=False, wait=False, qos=QOS_STATUS, retain=RETAIN_STATUS
        )

    # ------------------------------------------------------------------
    # Sensor initialisation
    # ------------------------------------------------------------------

    def _init_sensors(self, wait: bool = True) -> None:
        """Load sensor config+state, reconcile with dongle, run migrations, publish discovery."""
        registry = self._registry
        registry.load_sensors()
        registry.load_state()

        # Reconcile with dongle's paired sensor list
        try:
            linked = self._dongle.list()
            if linked is not None:
                auto_added = registry.reconcile_with_dongle(linked)
                for mac in auto_added:
                    self._logger.warning(
                        f"Auto-added unconfigured sensor {mac} — "
                        f"update dongles/{self._dongle.mac}/sensors.yaml to set name/type"
                    )
            else:
                self._logger.warning("Dongle returned empty sensor list")
                registry.ensure_all_have_state()
        except TimeoutError:
            self._logger.error("Timed out fetching sensor list from dongle")
            registry.ensure_all_have_state()

        # Discovery schema migration (run once per schema bump)
        if self._config["hass_discovery"]:
            recorded = self._gateway.get_discovery_schema_version()
            if recorded < DISCOVERY_SCHEMA_VERSION:
                self._logger.info(
                    f"Migrating discovery topics v{recorded} → v{DISCOVERY_SCHEMA_VERSION} "
                    f"for dongle {self._dongle.mac}"
                )
                for mac in list(registry.state):
                    if registry.is_valid_mac(mac):
                        sensor_type = registry.sensors.get(mac, {}).get("sensor_type", "unknown")
                        self._gateway.migrate_discovery_topics(
                            mac, sensor_type, self._dongle.mac, self._hub_id, recorded, wait=wait
                        )
                # Version is written by Bridge after all workers have migrated

        # Publish discovery for all known sensors
        if self._config["hass_discovery"]:
            for mac in list(registry.state):
                if registry.is_valid_mac(mac):
                    self._publish_sensor_discovery(mac, wait=wait)

        # Subscribe to command topics for already-configured sensors
        if self._gateway.is_connected:
            for mac, sensor in list(registry.sensors.items()):
                if sensor.get("sensor_type") == "keypad":
                    self._subscribe_keypad_command(mac)
                elif sensor.get("sensor_type") == "chime":
                    self._subscribe_chime(mac)
                self._subscribe_sensor_config(mac)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _publish_sensor_discovery(self, mac: str, wait: bool = True) -> None:
        """Publish HA MQTT discovery and initial availability for one sensor."""
        sensor = self._registry.sensors.get(mac, {})
        online = self._registry.state.get(mac, {}).get("online", False)
        # For leak sensors, probe_available comes from the most recent event (stored in state).
        # Default True so first-publish before any event includes probe_state (it will be
        # corrected on first event if there is no probe).
        probe_available = self._registry.state.get(mac, {}).get("probe_available", True)
        self._gateway.publish_sensor_discovery(
            mac,
            sensor,
            self._dongle.mac,
            self._hub_id,
            sensor_online=online,
            wait=wait,
            probe_available=probe_available,
        )

    def _remove_sensor(self, mac: str, wait: bool = True) -> None:
        """Unpair from dongle, clear MQTT topics, remove from registry."""
        try:
            self._dongle.delete(mac)
        except TimeoutError:
            self._logger.error(f"Dongle timeout while removing {mac}")

        sensor_type = self._registry.sensors.get(mac, {}).get("sensor_type", "unknown")
        self._gateway.clear_sensor(mac, sensor_type, wait=wait)
        self._registry.delete_sensor(mac)

    def _valid_sensor_mac_or_delete(self, mac: str) -> bool:
        """Return True if *mac* is valid; otherwise unpair it from the dongle."""
        if self._registry.is_valid_mac(mac):
            return True
        self._logger.warning(f"Invalid MAC detected, removing from dongle: {mac!r}")
        try:
            self._dongle.delete(mac)
            self._gateway.clear_sensor(mac, "unknown")
        except TimeoutError:
            self._logger.error("Dongle timeout while removing invalid MAC")
        return False

    def _mac_from_topic(self, topic: str, expected_suffix: str) -> str | None:
        """Extract the sensor MAC from an MQTT topic.

        Strips the configured self_topic_root prefix, the "sensor/" segment,
        and a known trailing suffix.  Works correctly even when self_topic_root
        contains slashes.

        Example:
            root = "home/ws2m", topic = "home/ws2m/sensor/AABBCCDD/set", suffix = "/set"
            → "AABBCCDD"

        Returns None and logs a warning if the topic does not match.
        """
        cfg = self._config
        prefix = f"{cfg['self_topic_root']}/sensor/"
        if not topic.startswith(prefix):
            self._logger.warning(f"Topic {topic!r} does not start with expected prefix {prefix!r}")
            return None
        remainder = topic[len(prefix) :]  # e.g. "AABBCCDD/set"
        if not remainder.endswith(expected_suffix):
            self._logger.warning(f"Topic {topic!r} does not end with expected suffix {expected_suffix!r}")
            return None
        mac = remainder[: -len(expected_suffix)]
        if "/" in mac:
            self._logger.warning(f"Could not extract MAC from topic {topic!r}: unexpected path structure")
            return None
        return mac

    # ------------------------------------------------------------------
    # MQTT command callbacks — dongle-scoped
    # ------------------------------------------------------------------

    def _on_mqtt_scan(self, client, userdata, msg) -> None:
        self._logger.info(f"Scan requested on dongle {self._dongle.mac}: {msg.payload.decode()!r}")
        result = None
        try:
            result = self._dongle.scan()
        except TimeoutError:
            self._logger.warning(f"Scan timed out on dongle {self._dongle.mac} — no sensor found")

        if result:
            mac, sensor_type, sensor_version = result
            self._logger.info(f"Scan found: mac={mac} type={sensor_type} ver={sensor_version}")
            if self._valid_sensor_mac_or_delete(mac):
                if mac not in self._registry.sensors:
                    self._registry.add_sensor(mac, sensor_type, sensor_version)
                    if self._config["hass_discovery"]:
                        self._publish_sensor_discovery(mac, wait=False)
                    self._subscribe_sensor_config(mac)
                else:
                    self._logger.debug(f"Scan: sensor {mac} already in registry — no action needed")
        else:
            self._logger.info("Scan: no new sensor found")

    def _on_mqtt_remove(self, client, userdata, msg) -> None:
        mac = msg.payload.decode()
        self._logger.info(f"Remove requested on dongle {self._dongle.mac}: {mac!r}")
        if self._valid_sensor_mac_or_delete(mac):
            self._remove_sensor(mac, wait=False)

    # ------------------------------------------------------------------
    # Sensor config MQTT (name, device_class, invert_state)
    # ------------------------------------------------------------------

    def _subscribe_sensor_config(self, mac: str) -> None:
        """Subscribe to sensor configuration command topics for one sensor."""
        cfg = self._config
        mac_topic = f"{cfg['self_topic_root']}/sensor/{mac}"

        topics = {
            f"{mac_topic}/sensor_name/set": self._on_mqtt_sensor_name,
            f"{mac_topic}/device_class/set": self._on_mqtt_device_class,
            f"{mac_topic}/invert_state/set": self._on_mqtt_invert_state,
        }

        for topic, callback in topics.items():
            if topic not in self._sensor_config_subscribed:
                self._sensor_config_subscribed.add(topic)
                self._gateway.client.subscribe(topic, QOS_COMMAND)
                self._gateway.client.message_callback_add(topic, callback)
                self._logger.debug(f"Subscribed to sensor config topic: {topic}")

    def _on_mqtt_sensor_name(self, client, userdata, msg) -> None:
        """Handle a sensor name update from HA."""
        cfg = self._config
        mac = self._mac_from_topic(msg.topic, "/sensor_name/set")
        if mac is None or mac not in self._registry.sensors:
            return
        new_name = msg.payload.decode(errors="replace").strip()
        if not new_name:
            return
        old_name = self._registry.sensors[mac].get("name", "")
        if new_name == old_name:
            return
        self._logger.info(f"Sensor {mac}: name → {new_name!r}")
        self._registry.sensors[mac]["name"] = new_name
        self._registry.save_sensors()
        # Re-publish discovery so HA device name updates
        if cfg["hass_discovery"]:
            self._publish_sensor_discovery(mac, wait=False)
        # Echo state back
        self._gateway.publish(
            f"{cfg['self_topic_root']}/sensor/{mac}/sensor_name",
            new_name,
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )

    def _on_mqtt_device_class(self, client, userdata, msg) -> None:
        """Handle a device_class select update from HA."""
        cfg = self._config
        mac = self._mac_from_topic(msg.topic, "/device_class/set")
        if mac is None or mac not in self._registry.sensors:
            return
        sensor = self._registry.sensors[mac]
        sensor_type = sensor.get("sensor_type", "unknown")
        if sensor_type not in DEVICE_CLASS_OPTIONS:
            return
        new_class = msg.payload.decode(errors="replace").strip()
        if new_class not in DEVICE_CLASS_OPTIONS[sensor_type]:
            self._logger.warning(f"Sensor {mac}: invalid device_class {new_class!r} for type {sensor_type!r}")
            return
        if sensor.get("class") == new_class:
            return
        self._logger.info(f"Sensor {mac}: device_class → {new_class!r}")
        sensor["class"] = new_class
        self._registry.save_sensors()
        # Re-publish discovery so HA entity updates device_class
        if cfg["hass_discovery"]:
            self._publish_sensor_discovery(mac, wait=False)
        # Echo state back
        self._gateway.publish(
            f"{cfg['self_topic_root']}/sensor/{mac}/device_class",
            new_class,
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )

    def _on_mqtt_invert_state(self, client, userdata, msg) -> None:
        """Handle an invert_state switch update from HA."""
        cfg = self._config
        mac = self._mac_from_topic(msg.topic, "/invert_state/set")
        if mac is None or mac not in self._registry.sensors:
            return
        sensor = self._registry.sensors[mac]
        if sensor.get("sensor_type", "unknown") not in INVERTIBLE_SENSOR_TYPES:
            return
        raw = msg.payload.decode(errors="replace").strip().lower()
        new_val = raw in ("true", "on", "1", "yes")
        if sensor.get("invert_state") == new_val:
            return
        self._logger.info(f"Sensor {mac}: invert_state → {new_val}")
        sensor["invert_state"] = new_val
        self._registry.save_sensors()
        # Re-publish discovery so HA swaps payload_on/payload_off
        if cfg["hass_discovery"]:
            self._publish_sensor_discovery(mac, wait=False)
        # Echo state back
        self._gateway.publish(
            f"{cfg['self_topic_root']}/sensor/{mac}/invert_state",
            "true" if new_val else "false",
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )

    # ------------------------------------------------------------------
    # Keypad MQTT
    # ------------------------------------------------------------------

    def _subscribe_keypad_command(self, mac: str) -> None:
        """Subscribe to the alarm state command topic and PIN management topics for a keypad."""
        cfg = self._config
        mac_topic = f"{cfg['self_topic_root']}/sensor/{mac}"

        topics = {
            f"{mac_topic}/set": self._on_mqtt_keypad_command,
            f"{mac_topic}/add_pin": self._on_mqtt_keypad_add_pin,
            f"{mac_topic}/clear_pins": self._on_mqtt_keypad_clear_pins,
        }
        for topic, callback in topics.items():
            if mac not in self._keypad_command_topics:
                self._keypad_command_topics[mac] = f"{mac_topic}/set"
            self._gateway.client.subscribe(topic, QOS_COMMAND)
            self._gateway.client.message_callback_add(topic, callback)
            self._logger.debug(f"Subscribed to keypad topic: {topic}")

    def _on_mqtt_keypad_command(self, client, userdata, msg) -> None:
        """Handle an inbound command from HA / Alarmo on a keypad command topic.

        Reflects the commanded state back onto the keypad's state topic and
        sends CMD_SEND_KEYPAD_EVENT to update the keypad's display/LEDs.

        HA payload → dongle state byte mapping (AK5nowman/WyzeSense + PR #63):
          disarmed    → 0x01
          armed_home  → 0x02
          armed_away  → 0x03
          triggered   → 0x04
        """
        mac = self._mac_from_topic(msg.topic, "/set")
        if mac is None:
            return
        payload_str = msg.payload.decode(errors="replace").strip()
        self._logger.info(f"Keypad command for {mac}: {payload_str!r}")

        cfg = self._config
        self._gateway.publish(
            f"{cfg['self_topic_root']}/sensor/{mac}",
            {"alarm_mode": payload_str},
        )

        _HA_MODE_TO_STATE_BYTE: dict[str, int] = {
            "disarmed": 0x01,
            "armed_home": 0x02,
            "armed_away": 0x03,
            "triggered": 0x04,
        }
        state_byte = _HA_MODE_TO_STATE_BYTE.get(payload_str)
        if state_byte is not None and self._dongle is not None:
            try:
                self._dongle.send_keypad_status(mac, state_byte)
                self._logger.debug(f"Sent keypad status 0x{state_byte:02X} to {mac}")
            except Exception as exc:
                self._logger.warning(f"Failed to send keypad status to {mac}: {exc}")
        elif state_byte is None:
            self._logger.debug(f"No keypad state byte mapping for {payload_str!r}")

    def _on_mqtt_keypad_add_pin(self, client, userdata, msg) -> None:
        """Handle Add PIN button press — arm ws2m to capture the next hardware PIN entry."""
        mac = self._mac_from_topic(msg.topic, "/add_pin")
        if mac is None or mac not in self._registry.sensors:
            return
        self._logger.info(f"Keypad {mac}: PIN capture armed — enter PIN on keypad to add it")
        self._keypad_pin_capture[mac] = True

    def _on_mqtt_keypad_clear_pins(self, client, userdata, msg) -> None:
        """Handle Clear PINs button press — wipe all configured PINs."""
        mac = self._mac_from_topic(msg.topic, "/clear_pins")
        if mac is None:
            return
        cleared = self._registry.clear_pins(mac)
        if cleared:
            self._logger.info(f"Keypad {mac}: all PINs cleared")
            cfg = self._config
            # Update pin_count state topic
            self._gateway.publish(
                f"{cfg['self_topic_root']}/sensor/{mac}/pin_count",
                "0",
                is_json=False,
                wait=False,
                qos=QOS_NUMBER,
                retain=RETAIN_NUMBER,
            )
        else:
            self._logger.info(f"Keypad {mac}: no PINs to clear")

    # ------------------------------------------------------------------
    # Chime MQTT
    # ------------------------------------------------------------------

    def _subscribe_chime(self, mac: str) -> None:
        """Subscribe to chime play and number set topics; publish current number states."""
        cfg = self._config
        mac_topic = f"{cfg['self_topic_root']}/sensor/{mac}"

        topics = {
            f"{mac_topic}/play": self._on_mqtt_chime_play,
            f"{mac_topic}/ring_id/set": self._on_mqtt_chime_number,
            f"{mac_topic}/volume/set": self._on_mqtt_chime_number,
            f"{mac_topic}/repeat_count/set": self._on_mqtt_chime_number,
        }

        for topic, callback in topics.items():
            if topic not in self._chime_subscribed:
                self._chime_subscribed.add(topic)
            self._gateway.client.subscribe(topic, QOS_COMMAND)
            self._gateway.client.message_callback_add(topic, callback)
            self._logger.debug(f"Subscribed to chime topic: {topic}")

        self._publish_chime_number_states(mac)

    def _publish_chime_number_states(self, mac: str) -> None:
        """Publish current ring_id / volume / repeat_count to HA number state topics."""
        cfg = self._config
        sensor_cfg = self._registry.sensors.get(mac, {})
        mac_topic = f"{cfg['self_topic_root']}/sensor/{mac}"
        pub = self._gateway.publish
        pub(
            f"{mac_topic}/ring_id",
            str(int(sensor_cfg.get("ring_id", 0))),
            is_json=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )
        pub(
            f"{mac_topic}/volume",
            str(int(sensor_cfg.get("volume", 5))),
            is_json=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )
        pub(
            f"{mac_topic}/repeat_count",
            str(int(sensor_cfg.get("repeat_count", 1))),
            is_json=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )

    def _on_mqtt_chime_play(self, client, userdata, msg) -> None:
        """Handle a chime play button press from HA."""
        mac = self._mac_from_topic(msg.topic, "/play")
        if mac is None:
            return
        sensor_cfg = self._registry.sensors.get(mac, {})
        ring_id = int(sensor_cfg.get("ring_id", 0))
        repeat_count = int(sensor_cfg.get("repeat_count", 1))
        volume = int(sensor_cfg.get("volume", 5))
        self._logger.info(f"Chime play: {mac} ring_id={ring_id} repeat={repeat_count} vol={volume}")
        if self._dongle is not None:
            try:
                self._dongle.play_chime(mac, ring_id, repeat_count, volume)
            except Exception as exc:
                self._logger.warning(f"Failed to play chime {mac}: {exc}")

    def _on_mqtt_chime_number(self, client, userdata, msg) -> None:
        """Handle a number entity update (ring_id, volume, or repeat_count) from HA."""
        cfg = self._config
        prefix = f"{cfg['self_topic_root']}/sensor/"
        if not msg.topic.startswith(prefix):
            self._logger.warning(f"Unexpected chime number topic: {msg.topic!r}")
            return
        remainder = msg.topic[len(prefix) :]  # "<mac>/<param>/set"
        parts = remainder.split("/")
        if len(parts) != 3 or parts[2] != "set":
            self._logger.warning(f"Unexpected chime number topic structure: {msg.topic!r}")
            return
        mac = parts[0]
        param = parts[1]

        try:
            raw = msg.payload.decode(errors="replace").strip()
            value = int(float(raw))
        except (ValueError, TypeError):
            self._logger.warning(f"Non-numeric chime {param} value: {msg.payload!r}")
            return

        if param == "ring_id":
            value = max(0, min(255, value))
        elif param in ("volume", "repeat_count"):
            value = max(1, min(9, value))

        sensor_cfg = self._registry.sensors.get(mac, {})
        if sensor_cfg.get(param) == value:
            return

        self._logger.info(f"Chime {mac}: {param} → {value}")
        self._registry.sensors.setdefault(mac, {})[param] = value
        self._registry.save_sensors()
        self._gateway.publish(
            f"{cfg['self_topic_root']}/sensor/{mac}/{param}",
            str(value),
            is_json=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )

    # ------------------------------------------------------------------
    # Dongle event handler
    # ------------------------------------------------------------------

    def _on_dongle_event(self, dongle, event) -> None:
        """Handle a sensor event from the dongle worker thread."""
        if not self._initialized:
            return

        if isinstance(event, str):
            self._logger.warning(f"Dongle message: {event}")
            return

        self._logger.debug(f"Sensor event from {event.mac}: {event}")

        if not self._valid_sensor_mac_or_delete(event.mac):
            return

        registry = self._registry
        cfg = self._config

        # Auto-add unseen sensor
        if event.mac not in registry.sensors:
            registry.add_sensor(event.mac, event.sensor_type if hasattr(event, "sensor_type") else None)
            if cfg["hass_discovery"]:
                self._publish_sensor_discovery(event.mac)
            self._subscribe_sensor_config(event.mac)
            if event.mac not in self._auto_add_warned:
                self._logger.warning(
                    f"Auto-added unconfigured sensor {event.mac} — "
                    f"update dongles/{self._dongle.mac}/sensors.yaml and reload to set name/type"
                )
                self._auto_add_warned.add(event.mac)
        else:
            if hasattr(event, "sensor_type"):
                changed = registry.update_sensor_type(event.mac, event.sensor_type)
                if changed:
                    self._logger.warning(f"Sensor type changed for {event.mac}")
                    if cfg["hass_discovery"]:
                        self._publish_sensor_discovery(event.mac)

        # Track probe_available for leak sensors; re-publish discovery when it changes.
        if hasattr(event, "probe_available"):
            prev = registry.state[event.mac].get("probe_available")
            registry.state[event.mac]["probe_available"] = event.probe_available
            if prev != event.probe_available and cfg["hass_discovery"]:
                self._publish_sensor_discovery(event.mac)

        # Ensure keypad/chime topics are subscribed on first event
        if getattr(event, "sensor_type", None) == "keypad":
            self._subscribe_keypad_command(event.mac)
        if getattr(event, "sensor_type", None) == "chime":
            self._subscribe_chime(event.mac)

        # Update last_seen and flip online flag
        registry.state[event.mac]["last_seen"] = event.timestamp
        if not registry.state[event.mac]["online"]:
            registry.state[event.mac]["online"] = True
            _name = registry.sensors.get(event.mac, {}).get("name", "")
            _label = f" ({_name})" if _name else ""
            self._logger.info(f"{event.mac}{_label} is back online")

        self._gateway.publish(
            f"{cfg['self_topic_root']}/sensor/{event.mac}/status",
            "online",
            is_json=False,
            qos=QOS_STATUS,
            retain=RETAIN_STATUS,
        )

        # Route keypad sub-events
        if event.event == "keypad_mode":
            self._gateway.publish(
                f"{cfg['self_topic_root']}/sensor/{event.mac}",
                {
                    "alarm_mode": event.alarm_mode,
                    "battery": getattr(event, "battery", None),
                    "signal_strength": getattr(event, "signal_strength", None),
                },
            )
            return
        if event.event == "keypad_motion":
            self._gateway.publish(
                f"{cfg['self_topic_root']}/sensor/{event.mac}",
                {
                    "motion": event.motion,
                    "battery": getattr(event, "battery", None),
                    "signal_strength": getattr(event, "signal_strength", None),
                },
            )
            return
        if event.event == "keypad_pin_start":
            self._logger.debug(f"Keypad {event.mac}: PIN entry started")
            return
        if event.event == "keypad_pin_confirm":
            sensor_cfg = registry.sensors.get(event.mac, {})
            configured_pins = sensor_cfg.get("pins", [])
            if isinstance(configured_pins, str):
                configured_pins = [configured_pins]

            # If PIN capture is armed, absorb this PIN into the config
            if self._keypad_pin_capture.pop(event.mac, False):
                added = registry.add_pin(event.mac, event.pin)
                if added:
                    self._logger.info(f"Keypad {event.mac}: PIN captured and added")
                    # Update pin_count state
                    self._gateway.publish(
                        f"{cfg['self_topic_root']}/sensor/{event.mac}/pin_count",
                        str(registry.pin_count(event.mac)),
                        is_json=False,
                        wait=False,
                        qos=QOS_NUMBER,
                        retain=RETAIN_NUMBER,
                    )
                else:
                    self._logger.info(f"Keypad {event.mac}: PIN already configured, not added")
                # Re-load configured_pins for validation below
                configured_pins = registry.sensors.get(event.mac, {}).get("pins", [])
                if isinstance(configured_pins, str):
                    configured_pins = [configured_pins]

            pin_valid = not configured_pins or event.pin in configured_pins
            self._logger.info(f"Keypad {event.mac}: PIN confirmed — valid={pin_valid}")
            self._gateway.publish(
                f"{cfg['self_topic_root']}/sensor/{event.mac}/pin",
                {
                    "pin_valid": pin_valid,
                    "battery": getattr(event, "battery", None),
                    "signal_strength": getattr(event, "signal_strength", None),
                },
            )
            return
        if event.event == "keypad_alarm":
            self._gateway.publish(
                f"{cfg['self_topic_root']}/sensor/{event.mac}/alarm",
                {
                    "alarm_raw": getattr(event, "alarm_raw", None),
                    "battery": getattr(event, "battery", None),
                    "signal_strength": getattr(event, "signal_strength", None),
                },
            )
            return

        if event.event not in ("alarm", "status"):
            self._logger.debug(f"Ignoring non-data event type {event.event!r} from {event.mac}")
            return

        payload = {}
        payload.update(registry.sensors.get(event.mac, {}))
        payload.update(vars(event))
        # Compute battery percentage from voltage using per-sensor discharge curves.
        # SensorEvent provides battery_voltage (V); the % conversion requires SENSOR_TYPES
        # which lives in sensors.py (an application module, not the protocol library).
        if "battery_voltage" in payload and payload["battery_voltage"] is not None:
            sensor_type = payload.get("sensor_type")
            type_meta = _SENSOR_TYPES.get(sensor_type, {})
            v_min = type_meta.get("battery_v_min")
            v_max = type_meta.get("battery_v_max")
            voltage = payload["battery_voltage"]
            if v_min is not None and v_max is not None:
                pct = (voltage - v_min) / (v_max - v_min) * 100.0
                payload["battery"] = max(0, min(100, round(pct)))
            else:
                payload["battery"] = None
        payload["ws2m_version"] = VERSION
        payload["ws2m_discovery_schema"] = DISCOVERY_SCHEMA_VERSION
        self._gateway.publish(f"{cfg['self_topic_root']}/sensor/{event.mac}", payload)

    # ------------------------------------------------------------------
    # Per-loop operations (called from Bridge.run)
    # ------------------------------------------------------------------

    def check_health(self) -> None:
        """Check dongle error state; mark worker failed on hardware error.

        Does not raise — failure is recorded on ``self._failed`` so Bridge.run
        can decide how to respond based on the full worker picture.
        """
        if self._failed:
            return
        try:
            self._dongle.check_error()
        except Exception as exc:
            self._failed = True
            _name = f" ({self._dongle.mac})" if self._dongle and self._dongle.mac else ""
            self._logger.error(
                f"Dongle{_name} at {self._device_path} has failed and will no longer be polled: {exc}",
                exc_info=True,
            )
            cfg = self._config
            # Best-effort: publish dongle, remote device, and sensors offline.
            try:
                if self._gateway and self._dongle_status_topic:
                    self._gateway.publish(
                        self._dongle_status_topic,
                        "offline",
                        is_json=False,
                        qos=QOS_STATUS,
                        retain=RETAIN_STATUS,
                    )
                if self._gateway and self._remote_id:
                    self._gateway.publish(
                        f"{cfg['self_topic_root']}/remote/{self._remote_id}/status",
                        "offline",
                        is_json=False,
                        qos=QOS_STATUS,
                        retain=RETAIN_STATUS,
                    )
                if self._gateway and self._registry:
                    for mac, state in self._registry.state.items():
                        if state.get("online"):
                            self._gateway.publish(
                                f"{cfg['self_topic_root']}/sensor/{mac}/status",
                                "offline",
                                is_json=False,
                                qos=QOS_STATUS,
                                retain=RETAIN_STATUS,
                            )
                            state["online"] = False
                    self._registry.save_state()
            except Exception:
                self._logger.debug("Failed to publish offline status during dongle failure", exc_info=True)

    def check_sensor_availability(self) -> None:
        """Mark sensors offline when last_seen exceeds their timeout."""
        now = time.time()
        for mac, state in self._registry.state.items():
            if not state.get("online"):
                continue
            sensor = self._registry.sensors.get(mac, {})
            timeout = self._registry.timeout_for(sensor)
            if (now - state["last_seen"]) > timeout:
                cfg = self._config
                self._gateway.publish(
                    f"{cfg['self_topic_root']}/sensor/{mac}/status",
                    "offline",
                    is_json=False,
                    qos=QOS_STATUS,
                    retain=RETAIN_STATUS,
                )
                state["online"] = False
                _name = sensor.get("name", "")
                _label = f" ({_name})" if _name else ""
                self._logger.warning(f"{mac}{_label} has gone offline (no data for >{timeout / 3600:.1f}h)")

    def reconnect_remote(self, transport: dongle_protocol.Transport) -> None:
        """Replace the underlying transport after a relay reconnect.

        Stops the current dongle (which closes the old WebSocket), then
        re-opens the dongle via the new transport.  Called from Bridge when
        the relay for this dongle MAC sends a new auth message.
        """
        self._logger.info(f"Reconnecting remote dongle {self.dongle_mac} via new transport")
        if self._dongle:
            self._dongle.stop()
            self._dongle = None
        self._transport = transport
        try:
            self._open_dongle()
            self._init_sensors(wait=False)
            self._gateway.publish(
                self._dongle_status_topic, "online", is_json=False, qos=QOS_STATUS, retain=RETAIN_STATUS
            )
        except Exception:
            self._logger.error(f"Failed to reconnect remote dongle {self.dongle_mac}", exc_info=True)
            self._failed = True

    def reload(self) -> None:
        """Reload sensor config and state, re-run discovery."""
        self._auto_add_warned.clear()
        self._registry.save_state()
        self._init_sensors(wait=False)

    def stop(self) -> None:
        """Publish dongle and remote device offline, save state, stop dongle thread."""
        self._stopped = True
        if self._gateway and self._dongle:
            self._gateway.publish(
                self._dongle_status_topic, "offline", is_json=False, qos=QOS_STATUS, retain=RETAIN_STATUS
            )
        if self._gateway and self._remote_id and self._config:
            self._gateway.publish(
                f"{self._config['self_topic_root']}/remote/{self._remote_id}/status",
                "offline",
                is_json=False,
                qos=QOS_STATUS,
                retain=RETAIN_STATUS,
            )
        if self._dongle:
            self._dongle.stop()
        if self._registry:
            self._registry.save_state()
        self._logger.info(f"DongleWorker stopped — {self._device_path}")


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class Bridge:
    """Top-level orchestrator: config, MQTT, service identity, DongleWorker lifecycle."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger.getChild("bridge")
        self._config: dict = {}
        self._hub_id: str = ""
        self._gateway: MqttGateway | None = None
        self._workers: list[DongleWorker] = []
        self._workers_lock = threading.Lock()
        self._ws_listener = None
        self._ws_listener_thread: threading.Thread | None = None

        self._reload_topic = ""
        self._log_level_set_topic = ""
        self._cleanup_removed_dongles_topic = ""
        self._remote_pair_topic = ""
        self._hub_restart_topic = ""
        self._hub_dongle_topic = ""
        self._hub_ws_port_topic = ""
        self._hub_remote_pairing_timeout_topic = ""
        self._hub_mdns_topic = ""
        self._hub_ws_enabled_topic = ""

        # Pairing mode state
        self._remote_pairing_expires: float = 0.0
        self._remote_pairing_lock = threading.Lock()

        # Set by the MQTT restart callback to wake run() and trigger a clean exit
        self._restart_requested = threading.Event()

    @property
    def _is_remote_pairing_active(self) -> bool:
        """Return True if pairing mode is currently active."""
        return time.monotonic() < self._remote_pairing_expires

    # ------------------------------------------------------------------
    # Startup sequence
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Full startup: config → service ID → MQTT → service discovery → dongles."""
        self._logger.info("=" * 80)
        self._logger.info("WyzeSense2MQTT starting")

        self._load_config()
        self._hub_id = load_hub_id(self._logger)
        self._setup_hub_topics(self._hub_id)
        self._connect_mqtt()

        if self._config["hass_discovery"]:
            self._gateway.publish_hub_discovery(
                self._hub_id,
                self._config.get("log_level", "INFO"),
            )
            self._publish_hub_config_state()
        else:
            self._logger.info("hass_discovery disabled — clearing any previously-published discovery topics")

        # Determine which local device paths to open and whether to start the WS listener
        device_paths = self._resolve_device_paths()
        accept_remote = bool(self._config.get("hub_ws_enabled", False))
        if accept_remote:
            _ws_port = self._config.get("hub_ws_port", 8765)
            self._logger.info(f"Remote connections enabled (hub_ws_enabled=True) on port {_ws_port}")
        if not device_paths and not accept_remote:
            self._logger.warning(
                "No WyzeSense dongles found and no remote connections enabled. "
                "Hub is running with no active dongle. Configure dongle via HA or config."
            )

        # Start a DongleWorker for each local device
        for path in device_paths:
            worker = DongleWorker(path, self._gateway, self._config, self._hub_id, self._logger)
            worker.start()
            with self._workers_lock:
                self._workers.append(worker)

        # Start WebSocket listener for remote relay connections
        if accept_remote:
            self._start_ws_listener()

        # Snapshot local workers (WS listener may add remote workers concurrently later)
        with self._workers_lock:
            workers_snapshot = list(self._workers)

        # After all workers have run migrations, bump the schema version once.
        # If hass_discovery was disabled, clear all retained discovery topics now
        # that we know every dongle MAC and sensor MAC.
        if self._config["hass_discovery"]:
            recorded = self._gateway.get_discovery_schema_version()
            if recorded < DISCOVERY_SCHEMA_VERSION:
                self._gateway.set_discovery_schema_version(DISCOVERY_SCHEMA_VERSION)
        else:
            self._logger.info("Clearing retained HA discovery topics (hass_discovery is disabled)")
            remote_ids = [w._remote_id for w in workers_snapshot if w._remote_id]
            dongle_macs = [w.dongle_mac for w in workers_snapshot if w.dongle_mac]
            sensors = [
                (mac, sensor.get("sensor_type", "unknown"))
                for w in workers_snapshot
                if w._registry
                for mac, sensor in w._registry.sensors.items()
            ]
            self._gateway.clear_all_hass_discovery(self._hub_id, remote_ids, dongle_macs, sensors, wait=False)

        total_sensors = sum(len(w._registry.sensors) for w in workers_snapshot if w._registry)
        with self._workers_lock:
            n_workers = len(self._workers)
        self._logger.info(f"Bridge ready — {n_workers} dongle(s), {total_sensors} sensor(s) active")
        self._logger.info("WyzeSense2MQTT ready")
        _mark_healthy()
        self._publish_hub_health("healthy")
        self._publish_hub_counts()
        with self._workers_lock:
            remote_ids = {w._remote_id for w in self._workers if w._remote_id}
        for rid in remote_ids:
            self._publish_remote_dongle_count(rid)

    def _load_config(self) -> None:
        cfg, from_file = load_config(self._logger)
        if cfg is None:
            raise RuntimeError("Failed to load configuration — check config/config.yaml or environment variables")
        self._config = cfg

        if from_file is None or cfg != from_file:
            self._logger.debug("Writing updated config.yaml (new default keys added)")
            save_config(cfg, self._logger)

        # Hub topic strings are set in _setup_hub_topics() after hub_id is loaded.

    def _setup_hub_topics(self, hub_id: str) -> None:
        """Set hub-level MQTT topic strings once the hub UUID is known."""
        hub = f"{self._config['self_topic_root']}/hub/{hub_id}"
        self._reload_topic = f"{hub}/reload"
        self._log_level_set_topic = f"{hub}/log_level/set"
        self._cleanup_removed_dongles_topic = f"{hub}/cleanup_removed_dongles"
        self._remote_pair_topic = f"{hub}/remote_pair"
        self._hub_restart_topic = f"{hub}/restart/set"
        self._hub_dongle_topic = f"{hub}/dongle/set"
        self._hub_ws_port_topic = f"{hub}/ws_port/set"
        self._hub_remote_pairing_timeout_topic = f"{hub}/remote_pairing_timeout/set"
        self._hub_mdns_topic = f"{hub}/mdns/set"
        self._hub_ws_enabled_topic = f"{hub}/ws_enabled/set"

    def _resolve_device_paths(self) -> list[str]:
        """Return local_device_paths.

        "auto"          → detect all local WyzeSense dongles.
        "/dev/hidrawN"  → single explicit device path.
        """
        dongle = str(self._config.get("dongle", "auto"))
        if dongle.lower() == "auto":
            found = find_all_dongle_devices()
            if found:
                self._logger.info(f"Auto-detect: found {len(found)} dongle(s): {found}")
            else:
                self._logger.warning("Auto-detect: no WyzeSense dongles found")
            return found
        if dongle.startswith("/"):
            self._logger.info(f"Using explicit dongle path: {dongle}")
            return [dongle]
        self._logger.warning(f"Unrecognized dongle value {dongle!r} — skipping")
        return []

    def _start_ws_listener(self) -> None:
        """Start the WebSocket listener and optional mDNS advertisement."""
        from ws_listener import WebSocketListener

        port = int(self._config.get("hub_ws_port", 8765))
        remotes_path = pathlib.Path(_cfg_mod.CONFIG_DIR) / "remotes"
        self._ws_listener = WebSocketListener(
            port=port,
            hub_id=self._hub_id,
            hub_version=VERSION,
            remotes_path=remotes_path,
            get_pairing_active=lambda: self._is_remote_pairing_active,
            on_connection=self._on_remote_connection,
            logger=self._logger,
        )
        self._ws_listener_thread = threading.Thread(
            target=self._ws_listener.serve_forever,
            name="ws-listener",
            daemon=True,
        )
        self._ws_listener_thread.start()

        if self._config.get("hub_ws_mdns", True):
            self._ws_listener.start_mdns()

    def _on_remote_connection(self, transport: RemoteTransport) -> None:
        """Called from the WS listener thread when a remote completes auth.

        Idempotent: if a worker for this dongle MAC already exists, the new
        transport replaces the old one (reconnect scenario).  Otherwise a
        new DongleWorker is created and started.
        """
        mac = transport.dongle_mac
        remote_id = transport.remote_id

        # Publish / refresh remote device discovery
        cfg = self._config
        if cfg.get("hass_discovery"):
            remote_status_topic = f"{cfg['self_topic_root']}/remote/{remote_id}/status"
            self._gateway.publish(remote_status_topic, "online", is_json=False, qos=QOS_STATUS, retain=RETAIN_STATUS)
            self._gateway.publish_remote_discovery(remote_id, self._hub_id)

        # Subscribe to remote command topics (restart + remove + cleanup + dongle + log_level)
        self._subscribe_remote_restart(remote_id, transport)
        self._subscribe_remote_remove(remote_id)
        self._subscribe_remote_cleanup(remote_id)
        self._subscribe_remote_dongle(remote_id, transport)
        self._subscribe_remote_log_level(remote_id, transport)

        with self._workers_lock:
            for worker in self._workers:
                if worker.dongle_mac == mac:
                    self._logger.info(f"Remote reconnected for dongle {mac} — handing off new transport")
                    worker.reconnect_remote(transport)
                    return
        # New dongle from remote
        self._logger.info(f"New remote dongle {mac} connecting via remote {remote_id!r}")
        worker = DongleWorker(
            None,
            self._gateway,
            self._config,
            self._hub_id,
            self._logger,
            transport=transport,
            remote_id=remote_id,
            on_remote_health_change=self._on_remote_health_change,
        )
        try:
            worker.start()
        except Exception:
            self._logger.error(f"Failed to start DongleWorker for remote dongle {mac}", exc_info=True)
            return
        with self._workers_lock:
            self._workers.append(worker)
        self._publish_hub_counts()
        self._publish_remote_dongle_count(remote_id)

    def _connect_mqtt(self) -> None:
        self._gateway = MqttGateway(self._config, self._logger)

        def _on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                # Subscribe to service-level topics
                client.subscribe(self._reload_topic, QOS_COMMAND)
                client.message_callback_add(self._reload_topic, self._on_mqtt_reload)
                client.subscribe(self._log_level_set_topic, QOS_COMMAND)
                client.message_callback_add(self._log_level_set_topic, self._on_mqtt_log_level)
                client.subscribe(self._cleanup_removed_dongles_topic, QOS_COMMAND)
                client.message_callback_add(self._cleanup_removed_dongles_topic, self._on_mqtt_cleanup_removed_dongles)
                client.subscribe(self._remote_pair_topic, QOS_COMMAND)
                client.message_callback_add(self._remote_pair_topic, self._on_mqtt_remote_pair)
                client.subscribe(self._hub_restart_topic, QOS_COMMAND)
                client.message_callback_add(self._hub_restart_topic, self._on_mqtt_hub_restart)
                client.subscribe(self._hub_dongle_topic, QOS_COMMAND)
                client.message_callback_add(self._hub_dongle_topic, self._on_mqtt_hub_dongle)
                client.subscribe(self._hub_ws_port_topic, QOS_COMMAND)
                client.message_callback_add(self._hub_ws_port_topic, self._on_mqtt_hub_ws_port)
                client.subscribe(self._hub_remote_pairing_timeout_topic, QOS_COMMAND)
                client.message_callback_add(
                    self._hub_remote_pairing_timeout_topic,
                    self._on_mqtt_hub_remote_pairing_timeout,
                )
                client.subscribe(self._hub_mdns_topic, QOS_COMMAND)
                client.message_callback_add(self._hub_mdns_topic, self._on_mqtt_hub_mdns)
                client.subscribe(self._hub_ws_enabled_topic, QOS_COMMAND)
                client.message_callback_add(self._hub_ws_enabled_topic, self._on_mqtt_hub_ws_enabled)
                # Re-subscribe all worker topics (covers reconnect case)
                with self._workers_lock:
                    workers_now = list(self._workers)
                for worker in workers_now:
                    worker.resubscribe()
                # Re-publish hub config state on reconnect
                self._publish_hub_config_state()
                client.connected_flag = True
                host = self._config["mqtt_host"]
                port = self._config["mqtt_port"]
                client_id = self._config["mqtt_client_id"]
                self._logger.info(f"Connected to MQTT broker {host}:{port} as {client_id}")
            else:
                self._logger.warning(f"MQTT connection failed: {reason_code}")

        def _on_disconnect(client, userdata, flags, reason_code, properties):
            client.message_callback_remove(self._reload_topic)
            client.message_callback_remove(self._log_level_set_topic)
            client.message_callback_remove(self._cleanup_removed_dongles_topic)
            client.message_callback_remove(self._remote_pair_topic)
            client.message_callback_remove(self._hub_restart_topic)
            client.message_callback_remove(self._hub_dongle_topic)
            client.message_callback_remove(self._hub_ws_port_topic)
            client.message_callback_remove(self._hub_remote_pairing_timeout_topic)
            client.message_callback_remove(self._hub_mdns_topic)
            client.message_callback_remove(self._hub_ws_enabled_topic)
            client.connected_flag = False
            self._logger.info(f"Disconnected from MQTT broker (reason: {reason_code})")

        def _on_message(client, userdata, msg):
            self._logger.warning(f"Unhandled MQTT message on {msg.topic!r}: {msg.payload!r}")

        self._gateway.connect(_on_connect, _on_disconnect, _on_message)

    def _on_remote_health_change(self, remote_id: str, is_healthy: bool) -> None:
        """Called when a remote sends a health frame over WebSocket.

        Publishes healthy/degraded to ws2m/remote/<uuid>/health.
        Does NOT affect hub health.  Dongle connectivity is tracked separately
        on the dongle device (ws2m_dongle_<mac>) via the DongleWorker lifecycle.
        """
        if not (self._gateway and self._config and self._hub_id):
            return
        cfg = self._config
        health_state = "healthy" if is_healthy else "degraded"
        health_topic = f"{cfg['self_topic_root']}/remote/{remote_id}/health"
        self._gateway.publish(
            health_topic, health_state, is_json=False, wait=False, qos=QOS_STATUS, retain=RETAIN_STATUS
        )
        self._logger.debug(f"Remote {remote_id} health → {health_state}")

    def _publish_hub_health(self, state: str) -> None:
        """Publish hub health state to <self_topic_root>/hub/<hub_id>/health (retained)."""
        if self._gateway and self._config and self._hub_id:
            cfg = self._config
            self._gateway.publish(
                f"{cfg['self_topic_root']}/hub/{self._hub_id}/health",
                state,
                is_json=False,
                wait=False,
                qos=QOS_STATUS,
                retain=RETAIN_STATUS,
            )

    def _publish_hub_counts(self) -> None:
        """Publish local dongle count, remote dongle count, and remote count to hub topics.

        Uses ``not w.failed and not w._stopped`` as the liveness filter so counts drop
        correctly both when a worker fails mid-run and when Bridge.stop() shuts down cleanly.
        """
        if not (self._gateway and self._config and self._hub_id):
            return
        with self._workers_lock:
            live = [w for w in self._workers if not w.failed and not w._stopped]
        local_count = sum(1 for w in live if w._remote_id is None)
        remote_workers = [w for w in live if w._remote_id is not None]
        remote_dongle_count = len(remote_workers)
        remote_count = len({w._remote_id for w in remote_workers})
        hub = f"{self._config['self_topic_root']}/hub/{self._hub_id}"
        for topic, value in [
            (f"{hub}/connected_dongles", str(local_count)),
            (f"{hub}/remote_dongles", str(remote_dongle_count)),
            (f"{hub}/connected_remotes", str(remote_count)),
        ]:
            self._gateway.publish(topic, value, is_json=False, wait=False, qos=QOS_STATUS, retain=RETAIN_STATUS)

    def _publish_remote_dongle_count(self, remote_id: str) -> None:
        """Publish the count of working dongles for one remote to remote/<uuid>/connected_dongles.

        Counts all live workers (not failed, not stopped) sharing this remote_id, so
        a remote with multiple dongles reports the correct aggregate rather than a fixed '1'.
        """
        if not (self._gateway and self._config):
            return
        with self._workers_lock:
            count = sum(1 for w in self._workers if w._remote_id == remote_id and not w.failed and not w._stopped)
        self._gateway.publish(
            f"{self._config['self_topic_root']}/remote/{remote_id}/connected_dongles",
            str(count),
            is_json=False,
            wait=False,
            qos=QOS_STATUS,
            retain=RETAIN_STATUS,
        )

    # ------------------------------------------------------------------
    # MQTT command callbacks — service-level
    # ------------------------------------------------------------------

    def _on_mqtt_hub_restart(self, client, userdata, msg) -> None:
        """Handle a hub restart button press from HA.

        Signals the main run() loop to perform a clean shutdown, then exit
        so the container restart policy brings the process back up.  The
        actual stop() call happens on the main thread to avoid deadlocking
        paho-mqtt's network loop.
        """
        self._logger.warning("Restart requested via MQTT — signalling main thread")
        self._restart_requested.set()

    def _on_mqtt_remote_restart(self, remote_id: str, transport: RemoteTransport):
        """Return an MQTT callback that forwards a restart command to the named remote."""

        def _handler(client, userdata, msg) -> None:
            self._logger.warning(f"Forwarding restart request to remote {remote_id}")
            try:
                transport.send_restart()
            except Exception as exc:
                self._logger.error(f"Failed to send restart to remote {remote_id}: {exc}")

        return _handler

    def _subscribe_remote_restart(self, remote_id: str, transport: RemoteTransport) -> None:
        """Subscribe to the remote restart command topic for this remote."""
        if not (self._gateway and self._config):
            return
        cfg = self._config
        restart_topic = f"{cfg['self_topic_root']}/remote/{remote_id}/restart/set"
        try:
            client = self._gateway.client
            client.subscribe(restart_topic, QOS_COMMAND)
            client.message_callback_add(restart_topic, self._on_mqtt_remote_restart(remote_id, transport))
        except Exception as exc:
            self._logger.error(f"Failed to subscribe to remote restart topic for {remote_id}: {exc}")

    def _subscribe_remote_remove(self, remote_id: str) -> None:
        """Subscribe to the remote remove command topic for this remote."""
        if not (self._gateway and self._config):
            return
        cfg = self._config
        remove_topic = f"{cfg['self_topic_root']}/remote/{remote_id}/remove"
        try:
            client = self._gateway.client
            client.subscribe(remove_topic, QOS_COMMAND)
            client.message_callback_add(remove_topic, self._on_mqtt_remote_remove(remote_id))
        except Exception as exc:
            self._logger.error(f"Failed to subscribe to remote remove topic for {remote_id}: {exc}")

    def _on_mqtt_remote_remove(self, remote_id: str):
        """Return an MQTT callback that removes a remote and its full sensor/dongle chain."""

        def _handler(client, userdata, msg) -> None:
            self._logger.info(f"Remove requested for remote {remote_id}")
            self._remove_remote_chain(remote_id)

        return _handler

    def _subscribe_remote_cleanup(self, remote_id: str) -> None:
        """Subscribe to the remote cleanup_disconnected_dongles command topic."""
        if not (self._gateway and self._config):
            return
        cfg = self._config
        cleanup_topic = f"{cfg['self_topic_root']}/remote/{remote_id}/cleanup_disconnected_dongles"
        try:
            client = self._gateway.client
            client.subscribe(cleanup_topic, QOS_COMMAND)
            client.message_callback_add(cleanup_topic, self._on_mqtt_remote_cleanup_disconnected_dongles(remote_id))
        except Exception as exc:
            self._logger.error(f"Failed to subscribe to remote cleanup topic for {remote_id}: {exc}")

    def _on_mqtt_remote_cleanup_disconnected_dongles(self, remote_id: str):
        """Return an MQTT callback that cleans up failed/disconnected dongles for a remote."""

        def _handler(client, userdata, msg) -> None:
            self._logger.info(f"Cleanup disconnected dongles requested for remote {remote_id}")
            self._remote_cleanup_disconnected_dongles(remote_id)

        return _handler

    def _remote_cleanup_disconnected_dongles(self, remote_id: str) -> None:
        """Clear topics and data for failed or stopped workers belonging to the given remote.

        Finds all DongleWorkers for this remote that have failed or been stopped,
        walks sensor → dongle for each, clears MQTT topics, deletes data directories,
        and removes those workers from self._workers.

        Active (healthy, running) workers for this remote are untouched.
        """
        if not self._gateway:
            return

        with self._workers_lock:
            failed_workers = [w for w in self._workers if w._remote_id == remote_id and (w.failed or w._stopped)]
            if not failed_workers:
                # Also check data dir for known dongles with no worker at all
                pass

        if not failed_workers:
            self._logger.info(f"Cleanup remote {remote_id}: no failed/stopped workers — nothing to clean up")
            return

        self._logger.info(f"Cleanup remote {remote_id}: cleaning up {len(failed_workers)} disconnected dongle(s)")

        for worker in failed_workers:
            dongle_mac = worker.dongle_mac
            # Clear sensor topics first
            if worker._registry:
                for sensor_mac, sensor in list(worker._registry.sensors.items()):
                    sensor_type = sensor.get("sensor_type", "unknown")
                    self._logger.info(f"  Clearing sensor topics: {sensor_mac} ({sensor_type})")
                    self._gateway.clear_sensor(sensor_mac, sensor_type, wait=False)
            # Clear dongle topics
            if dongle_mac:
                self._gateway.clear_dongle(dongle_mac, wait=False)
                # Delete data directory
                dongle_dir = dongle_data_path(dongle_mac)
                try:
                    shutil.rmtree(dongle_dir)
                    self._logger.info(f"  Deleted data directory: {dongle_dir}")
                except OSError as exc:
                    self._logger.warning(f"  Could not delete {dongle_dir}: {exc}")

        with self._workers_lock:
            failed_set = set(id(w) for w in failed_workers)
            self._workers = [w for w in self._workers if id(w) not in failed_set]

        self._publish_hub_counts()
        self._publish_remote_dongle_count(remote_id)
        self._logger.info(f"Cleanup remote {remote_id}: complete")

    def _subscribe_remote_dongle(self, remote_id: str, transport: RemoteTransport) -> None:
        """Subscribe to the remote dongle config command topic for this remote."""
        if not (self._gateway and self._config):
            return
        cfg = self._config
        topic = f"{cfg['self_topic_root']}/remote/{remote_id}/dongle/set"
        try:
            client = self._gateway.client
            client.subscribe(topic, QOS_COMMAND)
            client.message_callback_add(topic, self._on_mqtt_remote_dongle(remote_id, transport))
        except Exception as exc:
            self._logger.error(f"Failed to subscribe to remote dongle topic for {remote_id}: {exc}")

    def _on_mqtt_remote_dongle(self, remote_id: str, transport: RemoteTransport):
        """Return an MQTT callback that forwards a dongle path update to the named remote."""

        def _handler(client, userdata, msg) -> None:
            value = msg.payload.decode(errors="replace").strip()
            self._logger.info(
                f"Forwarding dongle config {value!r} to remote {remote_id} — effective after remote restart"
            )
            try:
                transport.send_dongle(value)
            except Exception as exc:
                self._logger.error(f"Failed to send dongle config to remote {remote_id}: {exc}")
            # Echo state back so HA entity stays in sync
            if self._gateway and self._config:
                cfg = self._config
                self._gateway.publish(
                    f"{cfg['self_topic_root']}/remote/{remote_id}/dongle",
                    value,
                    is_json=False,
                    wait=False,
                    qos=QOS_NUMBER,
                    retain=RETAIN_NUMBER,
                )

        return _handler

    def _subscribe_remote_log_level(self, remote_id: str, transport: RemoteTransport) -> None:
        """Subscribe to the remote log level command topic for this remote."""
        if not (self._gateway and self._config):
            return
        cfg = self._config
        topic = f"{cfg['self_topic_root']}/remote/{remote_id}/log_level/set"
        try:
            client = self._gateway.client
            client.subscribe(topic, QOS_COMMAND)
            client.message_callback_add(topic, self._on_mqtt_remote_log_level(remote_id, transport))
        except Exception as exc:
            self._logger.error(f"Failed to subscribe to remote log level topic for {remote_id}: {exc}")

    def _on_mqtt_remote_log_level(self, remote_id: str, transport: RemoteTransport):
        """Return an MQTT callback that forwards a log level change to the named remote."""

        def _handler(client, userdata, msg) -> None:
            from mqtt import LOG_LEVEL_OPTIONS

            level = msg.payload.decode(errors="replace").strip().upper()
            if level not in LOG_LEVEL_OPTIONS:
                self._logger.warning(f"Invalid log level {level!r} for remote {remote_id} — ignoring")
                return
            self._logger.info(f"Forwarding log level {level!r} to remote {remote_id}")
            try:
                transport.send_log_level(level)
            except Exception as exc:
                self._logger.error(f"Failed to send log level to remote {remote_id}: {exc}")
            # Echo state back so HA select stays in sync
            if self._gateway and self._config:
                cfg = self._config
                self._gateway.publish(
                    f"{cfg['self_topic_root']}/remote/{remote_id}/log_level",
                    level,
                    is_json=False,
                    wait=False,
                    qos=QOS_NUMBER,
                    retain=RETAIN_NUMBER,
                )

        return _handler

    def _remove_remote_chain(self, remote_id: str) -> None:
        """Clear all MQTT topics, stop all workers, and delete the token for the given remote.

        Walks the chain sensor → dongle → remote so HA removes entities in the
        correct dependency order.  The remote's token file is deleted so the remote
        cannot reconnect without going through the pairing flow again.
        """
        if not self._gateway:
            return

        with self._workers_lock:
            remote_workers = [w for w in self._workers if w._remote_id == remote_id]

        if not remote_workers:
            self._logger.warning(f"Remove remote: no active workers found for {remote_id}")
        else:
            for worker in remote_workers:
                dongle_mac = worker.dongle_mac
                # Clear all sensors on this dongle
                if worker._registry:
                    for sensor_mac, sensor in list(worker._registry.sensors.items()):
                        sensor_type = sensor.get("sensor_type", "unknown")
                        self._logger.info(f"  Clearing sensor topics: {sensor_mac} ({sensor_type})")
                        self._gateway.clear_sensor(sensor_mac, sensor_type, wait=False)
                # Clear the dongle
                if dongle_mac:
                    self._gateway.clear_dongle(dongle_mac, wait=False)
                # Stop the worker
                worker.stop()

            with self._workers_lock:
                self._workers = [w for w in self._workers if w._remote_id != remote_id]

        # Clear the remote device topics regardless of whether workers were found
        self._gateway.clear_remote(remote_id, wait=False)

        # Delete the token file so the remote cannot reconnect without re-pairing
        remotes_path = pathlib.Path(_cfg_mod.CONFIG_DIR) / "remotes"
        token_file = remotes_path / remote_id / "token"
        try:
            if token_file.exists():
                token_file.unlink()
                self._logger.info(f"Deleted token for remote {remote_id}")
        except OSError as exc:
            self._logger.warning(f"Could not delete token for remote {remote_id}: {exc}")

        self._publish_hub_counts()
        self._logger.info(f"Remote {remote_id} removed")

    def _on_mqtt_reload(self, client, userdata, msg) -> None:
        self._logger.info(f"Reload requested: {msg.payload.decode()!r}")
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            worker.reload()

    def _on_mqtt_log_level(self, client, userdata, msg) -> None:
        """Handle a log level select change from HA."""
        cfg = self._config
        new_level = msg.payload.decode(errors="replace").strip().upper()
        if new_level not in LOG_LEVEL_OPTIONS:
            self._logger.warning(f"Invalid log level received: {new_level!r}")
            return
        current = cfg.get("log_level", "INFO").upper()
        if new_level == current:
            return
        self._logger.info(f"Log level changing: {current} → {new_level}")
        cfg["log_level"] = new_level
        save_config(cfg, self._logger)
        # Re-initialise logging at the new level
        init_logging(new_level)
        self._logger.info(f"Log level changed to {new_level}")
        # Echo state back so HA select stays in sync (now under ws2m/hub/<hub_id>/)
        self._gateway.publish(
            f"{cfg['self_topic_root']}/hub/{self._hub_id}/log_level",
            new_level,
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )

    def _publish_hub_config_state(self) -> None:
        """Publish current hub config values to all four config state topics (retained)."""
        if not (self._gateway and self._config and self._hub_id):
            return
        cfg = self._config
        hub = f"{cfg['self_topic_root']}/hub/{self._hub_id}"
        # dongle
        self._gateway.publish(
            f"{hub}/dongle",
            str(cfg.get("dongle", "auto")),
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )
        # ws_port
        self._gateway.publish(
            f"{hub}/ws_port",
            str(cfg.get("hub_ws_port", 8765)),
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )
        # remote_pairing_timeout
        self._gateway.publish(
            f"{hub}/remote_pairing_timeout",
            str(cfg.get("hub_remote_pairing_seconds", 60)),
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )
        # mdns
        self._gateway.publish(
            f"{hub}/mdns",
            "true" if cfg.get("hub_ws_mdns", True) else "false",
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )
        # ws_enabled
        self._gateway.publish(
            f"{hub}/ws_enabled",
            "true" if cfg.get("hub_ws_enabled", False) else "false",
            is_json=False,
            wait=False,
            qos=QOS_NUMBER,
            retain=RETAIN_NUMBER,
        )

    def _on_mqtt_hub_dongle(self, client, userdata, msg) -> None:
        """Handle dongle config update from HA."""
        cfg = self._config
        value = msg.payload.decode(errors="replace").strip()
        cfg["dongle"] = value
        save_config(cfg, self._logger)
        self._logger.info(f"Dongle config updated to {value!r} — effective after restart")
        if self._gateway and self._hub_id:
            self._gateway.publish(
                f"{cfg['self_topic_root']}/hub/{self._hub_id}/dongle",
                value,
                is_json=False,
                wait=False,
                qos=QOS_NUMBER,
                retain=RETAIN_NUMBER,
            )

    def _on_mqtt_hub_ws_port(self, client, userdata, msg) -> None:
        """Handle WebSocket port config update from HA."""
        cfg = self._config
        raw = msg.payload.decode(errors="replace").strip()
        try:
            value = int(float(raw))
        except (ValueError, TypeError):
            self._logger.warning(f"Invalid WebSocket port value: {raw!r}")
            return
        if not (1024 <= value <= 65535):
            self._logger.warning(f"WebSocket port {value} out of range 1024–65535 — ignoring")
            return
        cfg["hub_ws_port"] = value
        save_config(cfg, self._logger)
        self._logger.info(f"WebSocket port updated to {value} — effective after restart")
        if self._gateway and self._hub_id:
            self._gateway.publish(
                f"{cfg['self_topic_root']}/hub/{self._hub_id}/ws_port",
                str(value),
                is_json=False,
                wait=False,
                qos=QOS_NUMBER,
                retain=RETAIN_NUMBER,
            )

    def _on_mqtt_hub_remote_pairing_timeout(self, client, userdata, msg) -> None:
        """Handle remote pairing timeout config update from HA."""
        cfg = self._config
        raw = msg.payload.decode(errors="replace").strip()
        try:
            value = float(raw)
        except (ValueError, TypeError):
            self._logger.warning(f"Invalid remote pairing timeout value: {raw!r}")
            return
        if value < 10:
            self._logger.warning(f"Remote pairing timeout {value} below minimum 10 — ignoring")
            return
        cfg["hub_remote_pairing_seconds"] = value
        save_config(cfg, self._logger)
        self._logger.info(f"Remote pairing timeout updated to {value}s")
        if self._gateway and self._hub_id:
            self._gateway.publish(
                f"{cfg['self_topic_root']}/hub/{self._hub_id}/remote_pairing_timeout",
                str(value),
                is_json=False,
                wait=False,
                qos=QOS_NUMBER,
                retain=RETAIN_NUMBER,
            )

    def _on_mqtt_hub_mdns(self, client, userdata, msg) -> None:
        """Handle mDNS advertisement toggle from HA."""
        cfg = self._config
        raw = msg.payload.decode(errors="replace").strip().lower()
        enabled = raw == "true"
        cfg["hub_ws_mdns"] = enabled
        save_config(cfg, self._logger)

        if self._ws_listener is None:
            if enabled:
                # mDNS advertising a hub that isn't listening is misleading.
                # Keep hub_ws_mdns persisted as True so it auto-starts when
                # the listener is enabled later.
                self._logger.warning(
                    "mDNS: WebSocket listener is not running (hub_ws_enabled=False). "
                    "Enable the WebSocket listener first, then toggle mDNS."
                )
        elif enabled:
            self._ws_listener.start_mdns()
        else:
            self._ws_listener.stop_mdns()

        if self._gateway and self._hub_id:
            self._gateway.publish(
                f"{cfg['self_topic_root']}/hub/{self._hub_id}/mdns",
                "true" if enabled else "false",
                is_json=False,
                wait=False,
                qos=QOS_NUMBER,
                retain=RETAIN_NUMBER,
            )

    def _on_mqtt_hub_ws_enabled(self, client, userdata, msg) -> None:
        """Handle WebSocket listener enabled toggle from HA."""
        cfg = self._config
        raw = msg.payload.decode(errors="replace").strip().lower()
        enabled = raw == "true"
        cfg["hub_ws_enabled"] = enabled
        save_config(cfg, self._logger)

        if enabled and self._ws_listener is None:
            self._start_ws_listener()
        elif not enabled and self._ws_listener is not None:
            # stop() also tears down mDNS advertisement
            self._ws_listener.stop()
            self._ws_listener = None
            self._ws_listener_thread = None
            self._logger.info("WebSocket listener stopped")
            # Clean up all remote workers and their full topic chains.
            # When the listener is off, remotes can't connect, so clear everything
            # (sensor → dongle → remote) so HA doesn't show stale entities.
            with self._workers_lock:
                remote_ids = list({w._remote_id for w in self._workers if w._remote_id})
            for remote_id in remote_ids:
                self._logger.info(f"WebSocket disabled — removing remote chain for {remote_id}")
                self._remove_remote_chain(remote_id)

        if self._gateway and self._hub_id:
            self._gateway.publish(
                f"{cfg['self_topic_root']}/hub/{self._hub_id}/ws_enabled",
                "true" if enabled else "false",
                is_json=False,
                wait=False,
                qos=QOS_NUMBER,
                retain=RETAIN_NUMBER,
            )

    def _on_mqtt_remote_pair(self, client, userdata, msg) -> None:
        """Handle a pairing mode enable command from HA or CLI.

        Optional payload: number of seconds (overrides hub_remote_pairing_seconds).
        Publishes "active" to ws2m/hub/<uuid>/remote_pairing (retained) and sets a timer
        that publishes "inactive" when pairing mode expires.
        """
        payload = msg.payload.decode(errors="replace").strip()
        try:
            seconds = float(payload) if payload and payload.replace(".", "", 1).isdigit() else None
        except ValueError:
            seconds = None
        cfg = self._config
        if seconds is None or seconds <= 0:
            seconds = float(cfg.get("hub_remote_pairing_seconds", 60))

        with self._remote_pairing_lock:
            self._remote_pairing_expires = time.monotonic() + seconds

        self._logger.info(f"Pairing mode enabled for {seconds:.0f}s")
        if self._gateway:
            self._gateway.publish(
                f"{cfg['self_topic_root']}/hub/{self._hub_id}/remote_pairing",
                "active",
                is_json=False,
                wait=False,
                qos=QOS_STATUS,
                retain=RETAIN_STATUS,
            )

        # Schedule deactivation
        def _deactivate():
            time.sleep(seconds)
            with self._remote_pairing_lock:
                if not self._is_remote_pairing_active:
                    self._logger.info("Pairing mode expired")
                    if self._gateway:
                        self._gateway.publish(
                            f"{cfg['self_topic_root']}/hub/{self._hub_id}/remote_pairing",
                            "inactive",
                            is_json=False,
                            wait=False,
                            qos=QOS_STATUS,
                            retain=RETAIN_STATUS,
                        )

        t = threading.Thread(target=_deactivate, daemon=True, name="remote-pairing-timer")
        t.start()

    def _on_mqtt_cleanup_removed_dongles(self, client, userdata, msg) -> None:
        """Handle a 'Cleanup removed dongles' button press from HA.

        Diffs the dongles recorded on disk against the currently-connected
        USB devices.  For each dongle that is no longer present:

        1. Clears all retained MQTT topics for its sensors and the dongle device.
        2. Deletes the ``data/dongles/<mac>/`` directory tree.

        This is a one-way destructive action — the dongle and all its sensor
        data are permanently removed from ws2m.  The operation is idempotent:
        if all known dongles are currently connected it is a no-op.
        """
        self._logger.info("Cleanup removed dongles requested")

        known_macs = list_known_dongle_macs(self._logger)
        if not known_macs:
            self._logger.info("No known dongles recorded — nothing to clean up")
            return

        with self._workers_lock:
            workers_snapshot = list(self._workers)
        active_macs: set[str] = {w.dongle_mac for w in workers_snapshot if w.dongle_mac}

        removed_macs = [mac for mac in known_macs if mac not in active_macs]
        if not removed_macs:
            self._logger.info("All recorded dongles are currently active — nothing to clean up")
            return

        self._logger.info(f"Cleaning up {len(removed_macs)} removed dongle(s): {removed_macs}")

        for mac in removed_macs:
            # Load the sensor registry for this dongle so we can clear per-sensor topics
            registry = SensorRegistry(mac)
            registry.load_sensors()
            for sensor_mac, sensor in registry.sensors.items():
                sensor_type = sensor.get("sensor_type", "unknown")
                self._logger.info(f"  Clearing sensor topics: {sensor_mac} ({sensor_type})")
                self._gateway.clear_sensor(sensor_mac, sensor_type, wait=False)

            # Clear dongle-level MQTT topics
            self._gateway.clear_dongle(mac, wait=False)

            # Delete the data directory
            dongle_dir = dongle_data_path(mac)
            try:
                shutil.rmtree(dongle_dir)
                self._logger.info(f"  Deleted data directory: {dongle_dir}")
            except OSError as exc:
                self._logger.warning(f"  Could not delete {dongle_dir}: {exc}")

        self._logger.info("Cleanup removed dongles complete")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the main availability-check loop until interrupted."""
        try:
            while True:
                # Use wait() instead of sleep() so a restart request wakes us immediately.
                self._restart_requested.wait(timeout=5)

                if self._restart_requested.is_set():
                    self._logger.warning("Restart requested — performing clean shutdown")
                    raise KeyboardInterrupt

                with self._workers_lock:
                    workers = list(self._workers)

                any_failed = False
                for worker in workers:
                    worker.check_health()
                    if worker.failed:
                        any_failed = True

                if any_failed:
                    _mark_unhealthy()
                    self._publish_hub_health("degraded")
                    self._publish_hub_counts()
                    failed_remote_ids = {w._remote_id for w in workers if w.failed and w._remote_id}
                    for rid in failed_remote_ids:
                        self._publish_remote_dongle_count(rid)

                # All workers failed — idle without doing any work until restarted.
                # If there are no local workers (remote-only mode), skip this check.
                if workers and all(w.failed for w in workers):
                    self._logger.debug("All dongle workers failed — idling until container is restarted")
                    continue

                # Heartbeat — also catches a hung process via HEALTHCHECK.
                _mark_healthy()
                if not any_failed:
                    self._publish_hub_health("healthy")

                if not self._gateway.is_connected:
                    self._logger.warning("MQTT broker disconnected — awaiting reconnect")

                for worker in workers:
                    if not worker.failed:
                        worker.check_sensor_availability()

        except KeyboardInterrupt:
            self._logger.warning("Interrupted by user")
        except Exception:
            self._logger.error("Unexpected error in main loop", exc_info=True)
        finally:
            _mark_unhealthy()
            self._publish_hub_health("degraded")
            self.stop()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop all workers, WebSocket listener, disconnect MQTT, save state."""
        self._logger.info("Shutting down...")
        if self._ws_listener is not None:
            # stop() also tears down mDNS advertisement
            self._ws_listener.stop()
        with self._workers_lock:
            workers = list(self._workers)
        # Collect remote IDs before stopping so we can publish zero counts after.
        remote_ids = {w._remote_id for w in workers if w._remote_id}
        for worker in workers:
            worker.stop()  # sets _stopped = True; publishes dongle + remote status offline
        # Publish zero counts now that all workers are stopped.
        self._publish_hub_counts()
        for rid in remote_ids:
            self._publish_remote_dongle_count(rid)
        if self._gateway:
            self._gateway.disconnect()
        self._logger.info("WyzeSense2MQTT stopped")
