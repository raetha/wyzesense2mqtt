# Linux Systemd Installation

The gateway can be run as a systemd service for those not wanting to use Docker.
Requires Python 3.12 or newer.  You may need to run commands as root depending
on filesystem permissions.

> **Note:** This installation method is not actively tested.  Docker is the
> recommended and supported deployment method for 4.0 and later.  Please submit
> an issue or PR if you encounter problems with systemd installation.

## Steps

1. Plug the Wyze Sense Bridge into a USB port on the Linux host.

2. Pull down a copy of the repository:
```bash
cd /tmp
git clone https://github.com/raetha/wyzesense2mqtt.git
```

3. Create local application folder (adjust path to suit):
```bash
mv /tmp/wyzesense2mqtt/wyzesense2mqtt /opt/wyzesense2mqtt
rm -rf /tmp/wyzesense2mqtt
cd /opt/wyzesense2mqtt
mkdir config
```

4. Create a `config/config.yaml` file.  You must set at minimum `mqtt_host`.
   All other settings have working defaults:
```bash
cat > config/config.yaml << 'YAML'
mqtt_host: <your-broker>
mqtt_username: <user>
mqtt_password: <password>
log_level: INFO
YAML
```

5. Install dependencies:
```bash
pip3 install -r requirements.txt
```

6. Configure and start the systemd service:
```bash
# Edit only if your install path differs from /opt/wyzesense2mqtt
vim wyzesense2mqtt.service
sudo cp wyzesense2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start wyzesense2mqtt
sudo systemctl status wyzesense2mqtt
sudo systemctl enable wyzesense2mqtt   # start on boot
```

7. View logs via journalctl (logs go to stdout, captured by systemd automatically):
```bash
journalctl -u wyzesense2mqtt -f
```

8. Pair sensors following the instructions in [README.md](../README.md#pairing-a-sensor).

## Notable changes from 3.x

- **No `logs/` directory** — logs now go to stdout only, captured by journalctl.
  If you previously had a `config/logging.yaml` file it is no longer read;
  delete it to avoid confusion.  Use `log_level` in `config.yaml` to control
  verbosity.
- **No sample files** — `config.yaml` is no longer copied from a samples
  directory.  Create it directly as shown above or let the bridge create a
  default on first run (requires `mqtt_host` to be set via environment variable
  or the file).
- **Python 3.12+ required** — 4.0 uses `X | Y` union type hint syntax
  (PEP 604) and other features not available in earlier versions. Python 3.12
  is actively supported until October 2028.
