"""
Bridge orchestration for WyzeSense2MQTT.

Architecture
------------
Bridge        — top-level orchestrator: config, MQTT connection, service discovery,
                service-level MQTT commands (reload, log_level), DongleWorker lifecycle.
DongleWorker  — owns one physical dongle + its SensorRegistry; handles all
                sensor events and dongle-scoped MQTT commands (scan, remove, sensor config).

Each DongleWorker runs independently against the shared MqttGateway.  When
usb_dongle is "auto", all detected dongles get a worker; when it is an explicit
path, exactly one worker is created for that device.

Startup sequence
----------------
  Bridge.start()
    ├─ _load_config()
    ├─ _load_service_identity()
    ├─ _connect_mqtt()
    ├─ _publish_service_discovery()
    └─ for each dongle device:
         DongleWorker.start()
           ├─ _open_dongle()
           ├─ _migrate_legacy_sensor_files()   (flat → per-dongle dir, once)
           ├─ _publish_dongle_discovery()
           └─ _init_sensors()

MQTT topics (v2 schema)
-----------------------
  <root>/reload                           — service-level reload command
  <root>/log_level                        — service log level state (retained)
  <root>/log_level/set                    — service log level command
  <root>/cleanup_removed_dongles          — service cleanup removed dongles command
  <root>/dongle_<mac>/scan                — scan for new sensor on this dongle
  <root>/dongle_<mac>/remove              — remove sensor by MAC payload
  <root>/dongle_<mac>/status              — dongle online/offline (retained)
  <root>/<sensor_mac>/status             — sensor online/offline (retained)
  <root>/<sensor_mac>                    — sensor data payload
  <root>/<sensor_mac>/sensor_name        — sensor display name state (retained)
  <root>/<sensor_mac>/sensor_name/set    — sensor display name command
  <root>/<sensor_mac>/device_class       — HA device class state (retained)
  <root>/<sensor_mac>/device_class/set   — HA device class command
  <root>/<sensor_mac>/invert_state       — invert state switch state (retained)
  <root>/<sensor_mac>/invert_state/set   — invert state switch command
  <root>/<sensor_mac>/set                — keypad command (alarm state in)
  <root>/<sensor_mac>/pin                — keypad PIN confirm event
  <root>/<sensor_mac>/alarm              — keypad alarm sub-event
  <root>/<sensor_mac>/pin_count          — keypad PIN count state (retained)
  <root>/<sensor_mac>/add_pin            — keypad: arm PIN capture mode
  <root>/<sensor_mac>/clear_pins         — keypad: clear all PINs
  <root>/<sensor_mac>/play               — chime play button
  <root>/<sensor_mac>/<param>/set        — chime number set
"""

import logging
import pathlib
import shutil
import time

import dongle_protocol
from config import (
    VERSION,
    dongle_data_path,
    find_all_dongle_devices,
    init_logging,
    list_known_dongle_macs,
    load_config,
    load_service_id,
    migrate_legacy_sensor_files,
    save_config,
)
from mqtt import (
    _QOS_COMMAND,
    _QOS_NUMBER,
    _QOS_STATUS,
    _RETAIN_NUMBER,
    _RETAIN_STATUS,
    DISCOVERY_SCHEMA_VERSION,
    LOG_LEVEL_OPTIONS,
    MqttGateway,
)
from retrying import retry
from sensors import DEVICE_CLASS_OPTIONS, INVERTIBLE_SENSOR_TYPES, SensorRegistry

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
        device_path: str,
        gateway: MqttGateway,
        cfg: dict,
        service_id: str,
        logger: logging.Logger,
    ):
        self._device_path = device_path
        self._gateway = gateway
        self._config = cfg
        self._service_id = service_id
        self._logger = logger.getChild("worker")

        self._dongle: dongle_protocol.Dongle | None = None
        self._registry: SensorRegistry | None = None
        self._initialized = False
        self._failed = False

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
        root = cfg["self_topic_root"]
        self._scan_topic = f"{root}/dongle_{mac}/scan"
        self._remove_topic = f"{root}/dongle_{mac}/remove"
        self._dongle_status_topic = f"{root}/dongle_{mac}/status"

        # Publish dongle offline until fully initialised
        self._gateway.publish(
            self._dongle_status_topic, "offline", is_json=False, qos=_QOS_STATUS, retain=_RETAIN_STATUS
        )

        # Subscribe to dongle-scoped command topics
        self._subscribe_dongle_topics()

        if cfg["hass_discovery"]:
            self._gateway.publish_dongle_discovery(mac, self._dongle.version, self._service_id)

        self._init_sensors()

        self._initialized = True
        self._logger.info(f"DongleWorker ready — dongle={mac}  sensors={len(self._registry.sensors)}")
        self._gateway.publish(
            self._dongle_status_topic, "online", is_json=False, qos=_QOS_STATUS, retain=_RETAIN_STATUS
        )

    @retry(
        wait_exponential_multiplier=1000,
        wait_exponential_max=30000,
        retry_on_exception=lambda e: isinstance(e, OSError),
    )
    def _open_dongle(self) -> None:
        self._logger.info(f"Opening dongle at {self._device_path}")
        try:
            self._dongle = dongle_protocol.open_dongle(self._device_path, self._on_dongle_event, self._logger)
            self._logger.info(f"Dongle ready — MAC: {self._dongle.mac}  VER: {self._dongle.version}")
        except OSError as err:
            self._logger.warning(f"Could not open dongle at {self._device_path}: {err} — will retry")
            raise

    def _subscribe_dongle_topics(self) -> None:
        """Subscribe to scan/remove command topics for this dongle."""
        client = self._gateway.client
        client.subscribe(
            [
                (self._scan_topic, _QOS_COMMAND),
                (self._remove_topic, _QOS_COMMAND),
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
        for _mac, cmd_topic in list(self._keypad_command_topics.items()):
            self._gateway.client.subscribe(cmd_topic, _QOS_COMMAND)
            self._gateway.client.message_callback_add(cmd_topic, self._on_mqtt_keypad_command)
        for topic in list(self._chime_subscribed):
            self._gateway.client.subscribe(topic, _QOS_COMMAND)
        for mac, sensor in list(self._registry.sensors.items()):
            if sensor.get("sensor_type") == "chime":
                self._subscribe_chime(mac)
        for topic in list(self._sensor_config_subscribed):
            self._gateway.client.subscribe(topic, _QOS_COMMAND)
        for mac in list(self._registry.sensors.keys()):
            self._subscribe_sensor_config(mac)
        # Re-publish dongle status
        self._gateway.publish(
            self._dongle_status_topic, "online", is_json=False, wait=False, qos=_QOS_STATUS, retain=_RETAIN_STATUS
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
                            mac, sensor_type, self._dongle.mac, self._service_id, recorded, wait=wait
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
            self._service_id,
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
        self._gateway.clear_sensor_topics(mac, sensor_type, wait=wait)
        self._registry.delete_sensor(mac)

    def _valid_sensor_mac_or_delete(self, mac: str) -> bool:
        """Return True if *mac* is valid; otherwise unpair it from the dongle."""
        if self._registry.is_valid_mac(mac):
            return True
        self._logger.warning(f"Invalid MAC detected, removing from dongle: {mac!r}")
        try:
            self._dongle.delete(mac)
            self._gateway.clear_sensor_topics(mac, "unknown")
        except TimeoutError:
            self._logger.error("Dongle timeout while removing invalid MAC")
        return False

    def _mac_from_topic(self, topic: str, expected_suffix: str) -> str | None:
        """Extract the sensor MAC from an MQTT topic.

        Strips the configured self_topic_root prefix and a known trailing suffix.
        Works correctly even when self_topic_root contains slashes.

        Example:
            root = "home/ws2m", topic = "home/ws2m/AABBCCDD/set", suffix = "/set"
            → "AABBCCDD"

        Returns None and logs a warning if the topic does not match.
        """
        root = self._config["self_topic_root"]
        prefix = f"{root}/"
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
        mac_topic = f"{cfg['self_topic_root']}/{mac}"

        topics = {
            f"{mac_topic}/sensor_name/set": self._on_mqtt_sensor_name,
            f"{mac_topic}/device_class/set": self._on_mqtt_device_class,
            f"{mac_topic}/invert_state/set": self._on_mqtt_invert_state,
        }

        for topic, callback in topics.items():
            if topic not in self._sensor_config_subscribed:
                self._sensor_config_subscribed.add(topic)
                self._gateway.client.subscribe(topic, _QOS_COMMAND)
                self._gateway.client.message_callback_add(topic, callback)
                self._logger.debug(f"Subscribed to sensor config topic: {topic}")

    def _on_mqtt_sensor_name(self, client, userdata, msg) -> None:
        """Handle a sensor name update from HA."""
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
        if self._config["hass_discovery"]:
            self._publish_sensor_discovery(mac, wait=False)
        # Echo state back
        self._gateway.publish(
            f"{self._config['self_topic_root']}/{mac}/sensor_name",
            new_name,
            is_json=False,
            wait=False,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
        )

    def _on_mqtt_device_class(self, client, userdata, msg) -> None:
        """Handle a device_class select update from HA."""
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
        if self._config["hass_discovery"]:
            self._publish_sensor_discovery(mac, wait=False)
        # Echo state back
        self._gateway.publish(
            f"{self._config['self_topic_root']}/{mac}/device_class",
            new_class,
            is_json=False,
            wait=False,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
        )

    def _on_mqtt_invert_state(self, client, userdata, msg) -> None:
        """Handle an invert_state switch update from HA."""
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
        if self._config["hass_discovery"]:
            self._publish_sensor_discovery(mac, wait=False)
        # Echo state back
        self._gateway.publish(
            f"{self._config['self_topic_root']}/{mac}/invert_state",
            "true" if new_val else "false",
            is_json=False,
            wait=False,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
        )

    # ------------------------------------------------------------------
    # Keypad MQTT
    # ------------------------------------------------------------------

    def _subscribe_keypad_command(self, mac: str) -> None:
        """Subscribe to the alarm state command topic and PIN management topics for a keypad."""
        cfg = self._config
        mac_topic = f"{cfg['self_topic_root']}/{mac}"

        topics = {
            f"{mac_topic}/set": self._on_mqtt_keypad_command,
            f"{mac_topic}/add_pin": self._on_mqtt_keypad_add_pin,
            f"{mac_topic}/clear_pins": self._on_mqtt_keypad_clear_pins,
        }
        for topic, callback in topics.items():
            if mac not in self._keypad_command_topics:
                self._keypad_command_topics[mac] = f"{mac_topic}/set"
            self._gateway.client.subscribe(topic, _QOS_COMMAND)
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

        self._gateway.publish(
            f"{self._config['self_topic_root']}/{mac}",
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
            # Update pin_count state topic
            self._gateway.publish(
                f"{self._config['self_topic_root']}/{mac}/pin_count",
                "0",
                is_json=False,
                wait=False,
                qos=_QOS_NUMBER,
                retain=_RETAIN_NUMBER,
            )
        else:
            self._logger.info(f"Keypad {mac}: no PINs to clear")

    # ------------------------------------------------------------------
    # Chime MQTT
    # ------------------------------------------------------------------

    def _subscribe_chime(self, mac: str) -> None:
        """Subscribe to chime play and number set topics; publish current number states."""
        cfg = self._config
        mac_topic = f"{cfg['self_topic_root']}/{mac}"

        topics = {
            f"{mac_topic}/play": self._on_mqtt_chime_play,
            f"{mac_topic}/ring_id/set": self._on_mqtt_chime_number,
            f"{mac_topic}/volume/set": self._on_mqtt_chime_number,
            f"{mac_topic}/repeat_count/set": self._on_mqtt_chime_number,
        }

        for topic, callback in topics.items():
            if topic not in self._chime_subscribed:
                self._chime_subscribed.add(topic)
            self._gateway.client.subscribe(topic, _QOS_COMMAND)
            self._gateway.client.message_callback_add(topic, callback)
            self._logger.debug(f"Subscribed to chime topic: {topic}")

        self._publish_chime_number_states(mac)

    def _publish_chime_number_states(self, mac: str) -> None:
        """Publish current ring_id / volume / repeat_count to HA number state topics."""
        cfg = self._config
        sensor_cfg = self._registry.sensors.get(mac, {})
        mac_topic = f"{cfg['self_topic_root']}/{mac}"
        pub = self._gateway.publish
        pub(
            f"{mac_topic}/ring_id",
            str(int(sensor_cfg.get("ring_id", 0))),
            is_json=False,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
        )
        pub(
            f"{mac_topic}/volume",
            str(int(sensor_cfg.get("volume", 5))),
            is_json=False,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
        )
        pub(
            f"{mac_topic}/repeat_count",
            str(int(sensor_cfg.get("repeat_count", 1))),
            is_json=False,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
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
        root = self._config["self_topic_root"]
        prefix = f"{root}/"
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
            f"{self._config['self_topic_root']}/{mac}/{param}",
            str(value),
            is_json=False,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
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
            _name = registry.sensors.get(event.mac, {}).get("sensor_name", "")
            _label = f" ({_name})" if _name else ""
            self._logger.info(f"{event.mac}{_label} is back online")

        self._gateway.publish(
            f"{cfg['self_topic_root']}/{event.mac}/status",
            "online",
            is_json=False,
            qos=_QOS_STATUS,
            retain=_RETAIN_STATUS,
        )

        # Route keypad sub-events
        if event.event == "keypad_mode":
            self._gateway.publish(
                f"{cfg['self_topic_root']}/{event.mac}",
                {
                    "alarm_mode": event.alarm_mode,
                    "battery": getattr(event, "battery", None),
                    "signal_strength": getattr(event, "signal_strength", None),
                },
            )
            return
        if event.event == "keypad_motion":
            self._gateway.publish(
                f"{cfg['self_topic_root']}/{event.mac}",
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
                        f"{cfg['self_topic_root']}/{event.mac}/pin_count",
                        str(registry.pin_count(event.mac)),
                        is_json=False,
                        wait=False,
                        qos=_QOS_NUMBER,
                        retain=_RETAIN_NUMBER,
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
                f"{cfg['self_topic_root']}/{event.mac}/pin",
                {
                    "pin_valid": pin_valid,
                    "battery": getattr(event, "battery", None),
                    "signal_strength": getattr(event, "signal_strength", None),
                },
            )
            return
        if event.event == "keypad_alarm":
            self._gateway.publish(
                f"{cfg['self_topic_root']}/{event.mac}/alarm",
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
        payload["ws2m_version"] = VERSION
        payload["ws2m_discovery_schema"] = DISCOVERY_SCHEMA_VERSION
        self._gateway.publish(f"{cfg['self_topic_root']}/{event.mac}", payload)

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
            root = cfg["self_topic_root"]
            # Best-effort: publish dongle and sensors offline.
            try:
                if self._gateway and self._dongle_status_topic:
                    self._gateway.publish(
                        self._dongle_status_topic,
                        "offline",
                        is_json=False,
                        qos=_QOS_STATUS,
                        retain=_RETAIN_STATUS,
                    )
                if self._gateway and self._registry:
                    for mac, state in self._registry.state.items():
                        if state.get("online"):
                            self._gateway.publish(
                                f"{root}/{mac}/status",
                                "offline",
                                is_json=False,
                                qos=_QOS_STATUS,
                                retain=_RETAIN_STATUS,
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
                self._gateway.publish(
                    f"{self._config['self_topic_root']}/{mac}/status",
                    "offline",
                    is_json=False,
                    qos=_QOS_STATUS,
                    retain=_RETAIN_STATUS,
                )
                state["online"] = False
                _name = sensor.get("sensor_name", "")
                _label = f" ({_name})" if _name else ""
                self._logger.warning(f"{mac}{_label} has gone offline (no data for >{timeout / 3600:.1f}h)")

    def reload(self) -> None:
        """Reload sensor config and state, re-run discovery."""
        self._auto_add_warned.clear()
        self._registry.save_state()
        self._init_sensors(wait=False)

    def stop(self) -> None:
        """Publish dongle offline, save state, stop dongle thread."""
        if self._gateway and self._dongle:
            self._gateway.publish(
                self._dongle_status_topic, "offline", is_json=False, qos=_QOS_STATUS, retain=_RETAIN_STATUS
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
        self._service_id: str = ""
        self._gateway: MqttGateway | None = None
        self._workers: list[DongleWorker] = []

        self._reload_topic = ""
        self._log_level_set_topic = ""
        self._cleanup_removed_dongles_topic = ""

    # ------------------------------------------------------------------
    # Startup sequence
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Full startup: config → service ID → MQTT → service discovery → dongles."""
        self._logger.info("=" * 80)
        self._logger.info("WyzeSense2MQTT starting")

        self._load_config()
        self._service_id = load_service_id(self._logger)
        self._connect_mqtt()

        if self._config["hass_discovery"]:
            self._gateway.publish_service_discovery(
                self._service_id,
                self._config.get("log_level", "INFO"),
            )
        else:
            self._logger.info("hass_discovery disabled — clearing any previously-published discovery topics")

        # Determine which device paths to open
        device_paths = self._resolve_device_paths()
        if not device_paths:
            raise RuntimeError("No WyzeSense dongles found — check USB connections")

        # Start a DongleWorker for each device
        for path in device_paths:
            worker = DongleWorker(path, self._gateway, self._config, self._service_id, self._logger)
            worker.start()
            self._workers.append(worker)

        # After all workers have run migrations, bump the schema version once.
        # If hass_discovery was disabled, clear all retained discovery topics now
        # that we know every dongle MAC and sensor MAC.
        if self._config["hass_discovery"]:
            recorded = self._gateway.get_discovery_schema_version()
            if recorded < DISCOVERY_SCHEMA_VERSION:
                self._gateway.set_discovery_schema_version(DISCOVERY_SCHEMA_VERSION)
        else:
            self._logger.info("Clearing retained HA discovery topics (hass_discovery is disabled)")
            self._gateway.clear_service_discovery(self._service_id, wait=False)
            for worker in self._workers:
                if worker.dongle_mac:
                    self._gateway.clear_dongle_discovery(worker.dongle_mac, wait=False)
                    for mac, sensor in worker._registry.sensors.items():
                        sensor_type = sensor.get("sensor_type", "unknown")
                        self._gateway.clear_all_sensor_discovery_topics(mac, sensor_type, wait=False)

        total_sensors = sum(len(w._registry.sensors) for w in self._workers)
        self._logger.info(f"Bridge ready — {len(self._workers)} dongle(s), {total_sensors} sensor(s) active")
        self._logger.info("WyzeSense2MQTT ready")
        _mark_healthy()

    def _load_config(self) -> None:
        cfg, from_file = load_config(self._logger)
        if cfg is None:
            raise RuntimeError("Failed to load configuration — check config/config.yaml or environment variables")
        self._config = cfg

        if from_file is None or cfg != from_file:
            self._logger.debug("Writing updated config.yaml (new default keys added)")
            save_config(cfg, self._logger)

        root = cfg["self_topic_root"]
        self._reload_topic = f"{root}/reload"
        self._log_level_set_topic = f"{root}/log_level/set"
        self._cleanup_removed_dongles_topic = f"{root}/cleanup_removed_dongles"

    def _resolve_device_paths(self) -> list[str]:
        """Return the list of USB device paths to open.

        "auto" → detect all WyzeSense dongles (multi-dongle supported).
        Any explicit path → single device, that path only.
        """
        usb_dongle = self._config.get("usb_dongle", "auto")
        if str(usb_dongle).lower() == "auto":
            devices = find_all_dongle_devices()
            if not devices:
                self._logger.warning("Auto-detect: no WyzeSense dongles found")
            else:
                self._logger.info(f"Auto-detect: found {len(devices)} dongle(s): {devices}")
            return devices
        else:
            self._logger.info(f"Using explicit dongle path: {usb_dongle}")
            return [str(usb_dongle)]

    def _connect_mqtt(self) -> None:
        self._gateway = MqttGateway(self._config, self._logger)

        def _on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                # Subscribe to service-level topics
                client.subscribe(self._reload_topic, _QOS_COMMAND)
                client.message_callback_add(self._reload_topic, self._on_mqtt_reload)
                client.subscribe(self._log_level_set_topic, _QOS_COMMAND)
                client.message_callback_add(self._log_level_set_topic, self._on_mqtt_log_level)
                client.subscribe(self._cleanup_removed_dongles_topic, _QOS_COMMAND)
                client.message_callback_add(self._cleanup_removed_dongles_topic, self._on_mqtt_cleanup_removed_dongles)
                # Re-subscribe all worker topics (covers reconnect case)
                for worker in self._workers:
                    worker.resubscribe()
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
            client.connected_flag = False
            self._logger.info(f"Disconnected from MQTT broker (reason: {reason_code})")

        def _on_message(client, userdata, msg):
            self._logger.warning(f"Unhandled MQTT message on {msg.topic!r}: {msg.payload!r}")

        self._gateway.connect(_on_connect, _on_disconnect, _on_message)

    # ------------------------------------------------------------------
    # MQTT command callbacks — service-level
    # ------------------------------------------------------------------

    def _on_mqtt_reload(self, client, userdata, msg) -> None:
        self._logger.info(f"Reload requested: {msg.payload.decode()!r}")
        for worker in self._workers:
            worker.reload()

    def _on_mqtt_log_level(self, client, userdata, msg) -> None:
        """Handle a log level select change from HA."""
        new_level = msg.payload.decode(errors="replace").strip().upper()
        if new_level not in LOG_LEVEL_OPTIONS:
            self._logger.warning(f"Invalid log level received: {new_level!r}")
            return
        current = self._config.get("log_level", "INFO").upper()
        if new_level == current:
            return
        self._logger.info(f"Log level changing: {current} → {new_level}")
        self._config["log_level"] = new_level
        save_config(self._config, self._logger)
        # Re-initialise logging at the new level
        init_logging(new_level)
        self._logger.info(f"Log level changed to {new_level}")
        # Echo state back so HA select stays in sync
        self._gateway.publish(
            f"{self._config['self_topic_root']}/log_level",
            new_level,
            is_json=False,
            wait=False,
            qos=_QOS_NUMBER,
            retain=_RETAIN_NUMBER,
        )

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

        active_macs: set[str] = {w.dongle_mac for w in self._workers if w.dongle_mac}

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
                self._gateway.clear_sensor_topics(sensor_mac, sensor_type, wait=False)

            # Clear dongle-level MQTT topics
            self._gateway.clear_dongle_all_topics(mac, wait=False)

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
                time.sleep(5)

                for worker in self._workers:
                    worker.check_health()
                    if worker.failed:
                        _mark_unhealthy()

                # All workers failed — idle without doing any work until restarted.
                if all(w.failed for w in self._workers):
                    self._logger.debug("All dongle workers failed — idling until container is restarted")
                    continue

                # Heartbeat — also catches a hung process via HEALTHCHECK.
                _mark_healthy()

                if not self._gateway.is_connected:
                    self._logger.warning("MQTT broker disconnected — awaiting reconnect")

                for worker in self._workers:
                    if not worker.failed:
                        worker.check_sensor_availability()

        except KeyboardInterrupt:
            self._logger.warning("Interrupted by user")
        except Exception:
            self._logger.error("Unexpected error in main loop", exc_info=True)
        finally:
            _mark_unhealthy()
            self.stop()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop all workers, disconnect MQTT, save state."""
        self._logger.info("Shutting down...")
        for worker in self._workers:
            worker.stop()
        if self._gateway:
            self._gateway.disconnect()
        self._logger.info("WyzeSense2MQTT stopped")
