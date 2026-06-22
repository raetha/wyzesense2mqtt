"""
Bridge orchestration for WyzeSense2MQTT.

The Bridge class ties together the three subsystems:
  - dongle_protocol.Dongle   – USB HID communication with the sensor hub
  - mqtt.MqttGateway         – MQTT broker connection and HA discovery
  - sensors.SensorRegistry   – per-sensor configuration and runtime state

It owns the main event loop, all MQTT command callbacks (scan/remove/reload),
and the dongle event handler.  It is also responsible for startup sequencing
and clean shutdown.
"""

import logging
import time

import dongle_protocol
from config import (
    VERSION,
    config_path,
    find_dongle_device,
    load_config,
    save_config,
)
from mqtt import DISCOVERY_SCHEMA_VERSION, MqttGateway
from retrying import retry
from sensors import SensorRegistry

# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class Bridge:
    """Orchestrates the dongle, MQTT gateway, and sensor registry."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger.getChild("bridge")
        self._config: dict = {}
        self._registry = SensorRegistry(logger)
        self._gateway: MqttGateway | None = None
        self._dongle: dongle_protocol.Dongle | None = None
        self._initialized = False

        # Track MACs we've already warned about (auto-add) so we don't
        # repeat the warning on every sensor event until the user reloads
        self._auto_add_warned: set[str] = set()

        # MQTT command topics (set after config is loaded)
        self._scan_topic = ""
        self._remove_topic = ""
        self._reload_topic = ""

        # Keypad command topics: mac → topic (populated as keypads are discovered)
        self._keypad_command_topics: dict[str, str] = {}

        # Chime command topics: mac → set of subscribed topics
        self._chime_subscribed: set[str] = set()

    # ------------------------------------------------------------------
    # Startup sequence
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Full startup: config → dongle → MQTT → discovery → sensor init."""
        self._logger.info("=" * 80)
        self._logger.info("WyzeSense2MQTT starting")

        self._load_config()
        self._open_dongle()
        self._connect_mqtt()
        self._init_bridge_discovery()
        self._init_sensors()

        self._initialized = True
        self._logger.info(f"Bridge ready — {len(self._registry.sensors)} sensor(s) active")
        cfg = self._config
        self._gateway.publish(
            f"{cfg['self_topic_root']}/bridge_{self._dongle.mac}/status",
            "online",
            is_json=False,
        )
        self._logger.info("WyzeSense2MQTT ready")

    def _load_config(self) -> None:
        cfg, from_file = load_config(self._logger)
        if cfg is None:
            raise RuntimeError("Failed to load configuration – check config/config.yaml or environment variables")
        self._config = cfg

        # Persist any newly-added default keys back to disk
        if from_file is None or cfg != from_file:
            self._logger.debug("Writing updated config.yaml (new default keys added)")
            save_config(cfg, self._logger)

        self._scan_topic = f"{cfg['self_topic_root']}/scan"
        self._remove_topic = f"{cfg['self_topic_root']}/remove"
        self._reload_topic = f"{cfg['self_topic_root']}/reload"

    @retry(
        wait_exponential_multiplier=1000,
        wait_exponential_max=30000,
        retry_on_exception=lambda e: isinstance(e, OSError),
    )
    def _open_dongle(self) -> None:
        cfg = self._config
        device = cfg["usb_dongle"]
        if device.lower() == "auto":
            device = find_dongle_device()
            if device:
                cfg["usb_dongle"] = device
            else:
                self._logger.warning("Auto-detect: no WyzeSense dongle found — will retry")
                raise OSError("No WyzeSense dongle found")

        self._logger.info(f"Opening dongle at {device}")
        try:
            self._dongle = dongle_protocol.open_dongle(device, self._on_dongle_event, self._logger)
            self._logger.info(f"Dongle ready — MAC: {self._dongle.mac}  VER: {self._dongle.version}")
        except OSError as err:
            self._logger.warning(f"Could not open dongle at {device}: {err} — will retry")
            raise

    def _connect_mqtt(self) -> None:
        self._gateway = MqttGateway(self._config, self._logger)

        # Mark bridge offline before fully initialising so HA doesn't see a
        # brief "online" blip during startup (the final publish happens in start())
        def _on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                client.subscribe(
                    [
                        (self._scan_topic, self._config["mqtt_qos"]),
                        (self._remove_topic, self._config["mqtt_qos"]),
                        (self._reload_topic, self._config["mqtt_qos"]),
                    ]
                )
                client.message_callback_add(self._scan_topic, self._on_mqtt_scan)
                client.message_callback_add(self._remove_topic, self._on_mqtt_remove)
                client.message_callback_add(self._reload_topic, self._on_mqtt_reload)
                # Re-subscribe to any keypad command topics (covers reconnect case)
                for _mac, cmd_topic in list(self._keypad_command_topics.items()):
                    client.subscribe(cmd_topic, self._config["mqtt_qos"])
                    client.message_callback_add(cmd_topic, self._on_mqtt_keypad_command)
                # Re-subscribe to chime number set topics (covers reconnect case)
                for topic in list(self._chime_subscribed):
                    client.subscribe(topic, self._config["mqtt_qos"])
                # Re-register chime callbacks and re-publish number states
                for mac, sensor in list(self._registry.sensors.items()):
                    if sensor.get("sensor_type") == "chime":
                        self._subscribe_chime(mac)
                client.connected_flag = True
                host = self._config["mqtt_host"]
                port = self._config["mqtt_port"]
                client_id = self._config["mqtt_client_id"]
                self._logger.info(f"Connected to MQTT broker {host}:{port} as {client_id}")
                # Publish online now that we're reconnected (covers reconnect case)
                self._gateway.publish(
                    f"{self._config['self_topic_root']}/bridge_{self._dongle.mac}/status",
                    "online",
                    is_json=False,
                    wait=False,
                )
            else:
                self._logger.warning(f"MQTT connection failed: {reason_code}")

        def _on_disconnect(client, userdata, flags, reason_code, properties):
            client.message_callback_remove(self._scan_topic)
            client.message_callback_remove(self._remove_topic)
            client.message_callback_remove(self._reload_topic)
            for cmd_topic in self._keypad_command_topics.values():
                client.message_callback_remove(cmd_topic)
            for topic in self._chime_subscribed:
                client.message_callback_remove(topic)
            client.connected_flag = False
            self._logger.info(f"Disconnected from MQTT broker (reason: {reason_code})")

        def _on_message(client, userdata, msg):
            self._logger.warning(f"Unhandled MQTT message on {msg.topic!r}: {msg.payload!r}")

        self._gateway.connect(_on_connect, _on_disconnect, _on_message)

        # Mark bridge offline until fully initialised
        self._gateway.publish(
            f"{self._config['self_topic_root']}/bridge_{self._dongle.mac}/status",
            "offline",
            is_json=False,
        )

    def _init_bridge_discovery(self) -> None:
        if not self._config["hass_discovery"]:
            return
        self._gateway.publish_bridge_discovery(
            self._dongle.mac,
            self._dongle.version,
        )

    def _init_sensors(self, wait: bool = True) -> None:
        """Load sensor config and state, reconcile with the dongle, and publish discovery.

        Called at startup and on reload.  Runs any pending discovery schema
        migrations before publishing so HA always sees up-to-date config topics.
        """
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
                        f"Auto-added unconfigured sensor {mac} — update sensors.yaml to set name/class"
                    )
            else:
                self._logger.warning("Dongle returned empty sensor list")
                registry.ensure_all_have_state()
        except TimeoutError:
            self._logger.error("Timed out fetching sensor list from dongle")
            registry.ensure_all_have_state()

        # Discovery schema migration (runs once per schema bump, covers sensors + bridge)
        if self._config["hass_discovery"]:
            recorded = self._gateway.get_discovery_schema_version()
            if recorded < DISCOVERY_SCHEMA_VERSION:
                self._logger.info(f"Migrating discovery topics v{recorded} → v{DISCOVERY_SCHEMA_VERSION}")
                for mac in list(registry.state):
                    if registry.is_valid_mac(mac):
                        sensor_type = registry.sensors.get(mac, {}).get("sensor_type", "unknown")
                        self._gateway.migrate_discovery_topics(mac, sensor_type, self._dongle.mac, recorded, wait=wait)
                self._gateway.set_discovery_schema_version(DISCOVERY_SCHEMA_VERSION)

        # Publish discovery for all known sensors
        if self._config["hass_discovery"]:
            for mac in list(registry.state):
                if registry.is_valid_mac(mac):
                    self._publish_sensor_discovery(mac, wait=wait)

        # Subscribe to command topics for any already-configured keypads and chimes
        for mac, sensor in list(registry.sensors.items()):
            if sensor.get("sensor_type") == "keypad" and self._gateway and self._gateway.is_connected:
                self._subscribe_keypad_command(mac)
            elif sensor.get("sensor_type") == "chime" and self._gateway and self._gateway.is_connected:
                self._subscribe_chime(mac)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _publish_sensor_discovery(self, mac: str, wait: bool = True) -> None:
        """Publish HA MQTT discovery and initial availability for one sensor."""
        sensor = self._registry.sensors.get(mac, {})
        online = self._registry.state.get(mac, {}).get("online", False)
        self._gateway.publish_sensor_discovery(
            mac,
            sensor,
            self._dongle.mac,
            sensor_online=online,
            wait=wait,
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

    # ------------------------------------------------------------------
    # MQTT command callbacks
    # ------------------------------------------------------------------

    def _on_mqtt_scan(self, client, userdata, msg) -> None:
        self._logger.info(f"Scan requested: {msg.payload.decode()!r}")
        result = None
        try:
            result = self._dongle.scan()
        except TimeoutError:
            pass

        if result:
            mac, sensor_type, sensor_version = result
            self._logger.info(f"Scan found: mac={mac} type={sensor_type} ver={sensor_version}")
            if self._valid_sensor_mac_or_delete(mac):
                if mac not in self._registry.sensors:
                    self._registry.add_sensor(mac, sensor_type, sensor_version)
                    if self._config["hass_discovery"]:
                        self._publish_sensor_discovery(mac, wait=False)
                else:
                    self._logger.debug(f"Scan: sensor {mac} already in registry — no action needed")
        else:
            self._logger.info("Scan: no new sensor found")

    def _on_mqtt_remove(self, client, userdata, msg) -> None:
        mac = msg.payload.decode()
        self._logger.info(f"Remove requested: {mac!r}")
        if self._valid_sensor_mac_or_delete(mac):
            self._remove_sensor(mac, wait=False)

    def _on_mqtt_reload(self, client, userdata, msg) -> None:
        self._logger.info(f"Reload requested: {msg.payload.decode()!r}")
        self._auto_add_warned.clear()
        self._registry.save_state()
        self._init_sensors(wait=False)

    def _subscribe_chime(self, mac: str) -> None:
        """Subscribe to the chime play and number set topics if not already done.

        Also publishes current ring_id / volume / repeat_count values to their
        respective state topics so HA number entities initialise correctly.
        """
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
            self._gateway.client.subscribe(topic, cfg["mqtt_qos"])
            self._gateway.client.message_callback_add(topic, callback)
            self._logger.debug(f"Subscribed to chime topic: {topic}")

        self._publish_chime_number_states(mac)

    def _publish_chime_number_states(self, mac: str) -> None:
        """Publish current ring_id / volume / repeat_count to their HA number state topics."""
        cfg = self._config
        sensor_cfg = self._registry.sensors.get(mac, {})
        mac_topic = f"{cfg['self_topic_root']}/{mac}"
        self._gateway.publish(f"{mac_topic}/ring_id", str(int(sensor_cfg.get("ring_id", 0))), is_json=False)
        self._gateway.publish(f"{mac_topic}/volume", str(int(sensor_cfg.get("volume", 5))), is_json=False)
        self._gateway.publish(f"{mac_topic}/repeat_count", str(int(sensor_cfg.get("repeat_count", 1))), is_json=False)

    def _mac_from_topic(self, topic: str, expected_suffix: str) -> str | None:
        """Extract the sensor MAC from an MQTT topic by stripping the configured
        self_topic_root prefix and a known trailing suffix.

        Works correctly even when self_topic_root contains slashes.

        Example:
            root = "home/ws2m", topic = "home/ws2m/AABBCCDD/set", suffix = "/set"
            → "AABBCCDD"

        Returns None and logs a warning if the topic does not match the expected pattern.
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
        # MAC must be a single path segment — reject if it contains a slash
        if "/" in mac:
            self._logger.warning(f"Could not extract MAC from topic {topic!r}: unexpected path structure")
            return None
        return mac

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
        """Handle a number entity update (ring_id, volume, or repeat_count) from HA.

        Persists the new value to sensors.yaml so it survives restarts, then
        echoes it back to the state topic so the HA entity reflects the change.
        """
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
        param = parts[1]  # "ring_id", "volume", or "repeat_count"

        try:
            raw = msg.payload.decode(errors="replace").strip()
            value = int(float(raw))  # HA may send "5.0" for integer sliders
        except (ValueError, TypeError):
            self._logger.warning(f"Non-numeric chime {param} value: {msg.payload!r}")
            return

        # Clamp to valid ranges
        if param == "ring_id":
            value = max(0, min(255, value))
        elif param in ("volume", "repeat_count"):
            value = max(1, min(9, value))

        sensor_cfg = self._registry.sensors.get(mac, {})
        if sensor_cfg.get(param) == value:
            return  # no change

        self._logger.info(f"Chime {mac}: {param} → {value}")
        self._registry.sensors.setdefault(mac, {})[param] = value
        self._registry.save_sensors()

        # Echo back to state topic
        cfg = self._config
        self._gateway.publish(
            f"{cfg['self_topic_root']}/{mac}/{param}",
            str(value),
            is_json=False,
        )

    def _subscribe_keypad_command(self, mac: str) -> None:
        """Subscribe to the command topic for a keypad sensor if not already subscribed.

        The command topic receives payloads from HA / Alarmo reflecting the
        current alarm state (e.g. "disarmed", "armed_away").  ws2m logs these
        and publishes them back to the keypad's state topic so HA entities
        stay consistent.  Future keypad firmware updates may use this to drive
        physical LED feedback.
        """
        cfg = self._config
        cmd_topic = f"{cfg['self_topic_root']}/{mac}/set"
        if mac not in self._keypad_command_topics:
            self._keypad_command_topics[mac] = cmd_topic
            self._gateway.client.subscribe(cmd_topic, cfg["mqtt_qos"])
            self._gateway.client.message_callback_add(cmd_topic, self._on_mqtt_keypad_command)
            self._logger.debug(f"Subscribed to keypad command topic: {cmd_topic}")

    def _on_mqtt_keypad_command(self, client, userdata, msg) -> None:
        """Handle an inbound command from HA / Alarmo on a keypad command topic.

        Publishes the new alarm state back to the keypad's state topic so the
        alarm_control_panel entity in HA reflects the current state, and sends
        CMD_SEND_KEYPAD_EVENT to the dongle so the keypad's display and LEDs
        update accordingly.

        HA/Alarmo payload → dongle state byte mapping (confirmed by PR + AK5nowman/WyzeSense):
          disarmed    → 0x01
          armed_home  → 0x02
          armed_away  → 0x03
          triggered   → 0x04
        """
        # Derive MAC from topic: <self_topic_root>/<mac>/set
        mac = self._mac_from_topic(msg.topic, "/set")
        if mac is None:
            return
        payload_str = msg.payload.decode(errors="replace").strip()
        self._logger.info(f"Keypad command received for {mac}: {payload_str!r}")

        # Reflect the commanded state back onto the state topic so HA stays in sync
        self._gateway.publish(
            f"{self._config['self_topic_root']}/{mac}",
            {"alarm_mode": payload_str},
        )

        # Map HA alarm state string → keypad state byte and push to dongle
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
            self._logger.debug(f"No keypad state byte mapping for {payload_str!r} — display not updated")

    # ------------------------------------------------------------------
    # Dongle event handler
    # ------------------------------------------------------------------

    def _on_dongle_event(self, dongle, event) -> None:
        """Handle a sensor event or diagnostic message from the dongle worker thread.

        Ignores events received before the bridge has finished initialising.
        Auto-adds any sensor not yet in the registry and publishes its reading
        to the MQTT state topic.
        """
        if not self._initialized:
            return

        # String events are diagnostic messages from the dongle worker
        if isinstance(event, str):
            self._logger.warning(f"Dongle message: {event}")
            return

        self._logger.debug(f"Sensor event from {event.mac}: {event}")

        if not self._valid_sensor_mac_or_delete(event.mac):
            return

        registry = self._registry
        cfg = self._config

        # Auto-add sensor if unseen
        if event.mac not in registry.sensors:
            registry.add_sensor(event.mac, event.sensor_type if hasattr(event, "sensor_type") else None)
            if cfg["hass_discovery"]:
                self._publish_sensor_discovery(event.mac)
            if event.mac not in self._auto_add_warned:
                self._logger.warning(
                    f"Auto-added unconfigured sensor {event.mac} — "
                    f"update {config_path('sensors.yaml')} and reload to set name/class"
                )
                self._auto_add_warned.add(event.mac)
        else:
            # Update sensor type if it has changed (e.g. firmware update)
            if hasattr(event, "sensor_type"):
                changed = registry.update_sensor_type(event.mac, event.sensor_type)
                if changed:
                    self._logger.warning(f"Sensor type changed for {event.mac}")
                    if cfg["hass_discovery"]:
                        self._publish_sensor_discovery(event.mac)

        # Ensure keypad command topic is subscribed whenever we see a keypad event
        if getattr(event, "sensor_type", None) == "keypad":
            self._subscribe_keypad_command(event.mac)

        # Ensure chime topics are subscribed whenever we see a chime heartbeat
        if getattr(event, "sensor_type", None) == "chime":
            self._subscribe_chime(event.mac)

        # Update last_seen and flip online flag if the sensor was previously offline
        registry.state[event.mac]["last_seen"] = event.timestamp
        if not registry.state[event.mac]["online"]:
            registry.state[event.mac]["online"] = True
            self._logger.info(f"{event.mac} is back online")

        self._gateway.publish(f"{cfg['self_topic_root']}/{event.mac}/status", "online", is_json=False)

        # Route keypad events to the appropriate MQTT payload
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
            # PIN start is informational only — no MQTT publish needed
            return

        if event.event == "keypad_pin_confirm":
            # Validate PIN against sensors.yaml if configured; always publish the event
            sensor_cfg = registry.sensors.get(event.mac, {})
            configured_pins = sensor_cfg.get("pins", [])
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
            # Sub-event 0x0C — exact semantics unknown; publish raw value for automations
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
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the main availability-check loop until interrupted."""
        try:
            while True:
                time.sleep(5)
                self._dongle.check_error()

                if not self._gateway.is_connected:
                    self._gateway.publish(
                        f"{self._config['self_topic_root']}/bridge_{self._dongle.mac}/status",
                        "offline",
                        is_json=False,
                    )

                self._check_sensor_availability()

        except KeyboardInterrupt:
            self._logger.warning("Interrupted by user")
        except Exception:
            self._logger.error("Unexpected error in main loop", exc_info=True)
        finally:
            self.stop()

    def _check_sensor_availability(self) -> None:
        """Mark sensors offline when their last_seen timestamp exceeds the timeout."""
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
                )
                state["online"] = False
                self._logger.warning(f"{mac} has gone offline (no data for >{timeout / 3600:.1f}h)")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Publish offline status, save state, disconnect cleanly."""
        self._logger.info("Shutting down...")

        if self._gateway and self._dongle:
            self._gateway.publish(
                f"{self._config['self_topic_root']}/bridge_{self._dongle.mac}/status",
                "offline",
                is_json=False,
            )

        if self._dongle:
            self._dongle.stop()

        if self._gateway:
            self._gateway.disconnect()

        self._registry.save_state()
        self._logger.info("WyzeSense2MQTT stopped")
