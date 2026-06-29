"""
ws2m remote — entry point.

Run as:
    python3 -m remote                    (from repository root)
    python3 __main__.py                  (from within remote/)

Configuration comes from environment variables (Docker-friendly) or
command-line flags.  Flags always take precedence over environment variables.

Environment variables
---------------------
WS2M_HUB_URL            Hub WebSocket URL                       [optional; auto-discovered via mDNS if unset]
WS2M_DISCOVERY_TIMEOUT  mDNS discovery timeout in seconds        [default: 30]
WS2M_HUB_ID             Preferred hub_id for mDNS selection      [optional]
WS2M_REMOTE_ID          Override stable remote UUID             [default: loaded from data dir]
WS2M_HUB_TOKEN          Hub token (set automatically after adoption)
WS2M_DATA_DIR           Data directory for remote_id/hub_token  [default: /app/data]
WS2M_DEVICE             HID device path or "auto"               [default: auto]
WS2M_QUEUE_MAX_SECONDS  Event frame TTL in seconds              [default: 10]
WS2M_QUEUE_MAX_FRAMES   Ring buffer maximum size                [default: 500]
WS2M_HANDSHAKE_FRAMES   Frames to classify as handshake         [default: 10]
LOG_LEVEL               Logging level (DEBUG/INFO/WARNING/...)  [default: INFO]
"""

import argparse
import logging
import os
import pathlib
import sys


def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ws2m-remote",
        description="ws2m remote — forward a local USB WyzeSense dongle to a ws2m hub over WebSocket",
    )
    parser.add_argument(
        "--hub",
        default=_env("WS2M_HUB_URL"),
        metavar="URL",
        help="Hub WebSocket URL (e.g. ws://192.168.1.10:8765)  [env: WS2M_HUB_URL]",
    )
    parser.add_argument(
        "--remote-id",
        default=_env("WS2M_REMOTE_ID"),
        metavar="UUID",
        help="Override stable remote UUID  [env: WS2M_REMOTE_ID]",
    )
    parser.add_argument(
        "--device",
        default=_env("WS2M_DEVICE", "auto"),
        metavar="PATH",
        help='HID device path or "auto"  [env: WS2M_DEVICE]',
    )
    parser.add_argument(
        "--log-level",
        default=_env("LOG_LEVEL", "INFO"),
        metavar="LEVEL",
        help="Logging level  [env: LOG_LEVEL]",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, (args.log_level or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)-25s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logger = logging.getLogger("ws2m")
    print("WyzeSense2MQTT remote starting — logs follow")

    from frame_queue import InMemoryFrameQueue

    from remote import Remote, _discover_hub_via_mdns, _load_or_create_remote_id

    hub_url = args.hub
    if not hub_url:
        logger.info("Discovering hub via mDNS...")
        timeout = float(_env("WS2M_DISCOVERY_TIMEOUT", "30") or 30)
        preferred_hub_id = _env("WS2M_HUB_ID")
        hub_url = _discover_hub_via_mdns(timeout=timeout, preferred_hub_id=preferred_hub_id, logger=logger)
        if not hub_url:
            logger.error("mDNS discovery timed out — set WS2M_HUB_URL to connect directly")
            sys.exit(1)

    data_dir = pathlib.Path(_env("WS2M_DATA_DIR", "/app/data") or "/app/data")
    data_dir.mkdir(parents=True, exist_ok=True)

    # Load or create the stable remote UUID (unless overridden via env/flag)
    remote_id = args.remote_id or _load_or_create_remote_id(data_dir)
    logger.info(f"Remote ID: {remote_id}")

    queue = InMemoryFrameQueue(
        max_seconds=float(_env("WS2M_QUEUE_MAX_SECONDS", "10") or 10),
        max_frames=int(_env("WS2M_QUEUE_MAX_FRAMES", "500") or 500),
    )

    remote = Remote(
        hub_url=hub_url,
        remote_id=remote_id,
        data_dir=data_dir,
        device=args.device or "auto",
        queue=queue,
        handshake_frame_count=int(_env("WS2M_HANDSHAKE_FRAMES", "10") or 10),
        logger=logger,
    )

    try:
        remote.run()
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
        remote.stop()


if __name__ == "__main__":
    main()
