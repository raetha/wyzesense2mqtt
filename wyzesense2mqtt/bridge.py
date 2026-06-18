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

        # Update last_seen and flip online flag if the sensor was previously offline
        registry.state[event.mac]["last_seen"] = event.timestamp
        if not registry.state[event.mac]["online"]:
            registry.state[event.mac]["online"] = True
            self._logger.info(f"{event.mac} is back online")

        self._gateway.publish(f"{cfg['self_topic_root']}/{event.mac}/status", "online", is_json=False)

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
