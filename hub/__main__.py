"""
WyzeSense2MQTT — entry point.

Run as:
    python3 __main__.py                (from within the hub/ directory)

Users should not invoke any other module directly.  See README.md for
Docker and systemd service usage.
"""

import signal
import sys
from pathlib import Path

# Dev/git-clone convenience: shared modules (dongle_protocol, device_discovery)
# live in ../shared when running from the repo tree.  Docker images and release
# packages flatten shared/ into the app directory, making this a no-op there.
_shared = Path(__file__).resolve().parent.parent / "shared"
if _shared.is_dir():
    sys.path.insert(0, str(_shared))

from config import init_logging, load_config  # noqa: E402


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
