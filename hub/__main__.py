"""
WyzeSense2MQTT — entry point.

Run as:
    python3 -m wyzesense2mqtt          (from package parent directory)
    python3 __main__.py                (from within the wyzesense2mqtt/ directory)

Users should not invoke any other module directly.  See README.md for
Docker and systemd service usage.
"""

import signal
import sys

from config import init_logging, load_config


def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _handle_sigterm)


def main() -> None:
    # Load config first so log_level is available before the bridge starts.
    # init_logging defaults to INFO if cfg is None or log_level is absent.
    cfg, _ = load_config()
    logger = init_logging(cfg.get("log_level") if cfg else None)
    print("WyzeSense2MQTT starting — logs follow")

    from bridge import Bridge, _mark_unhealthy

    bridge = Bridge(logger)
    try:
        bridge.start()
        bridge.run()
    except RuntimeError as err:
        logger.error(f"Fatal startup error: {err}")
        _mark_unhealthy()
        sys.exit(1)


if __name__ == "__main__":
    main()
