# Linux Systemd Installation

This document covers running `ws2m-hub` and `ws2m-remote` as native systemd services without Docker. Requires Python 3.12 or newer. You may need to run commands as root depending on filesystem permissions.

> **Note:** This installation method is not actively tested. Docker is the recommended and supported deployment method. Please submit an issue or PR if you encounter problems.

## Table of Contents
- [Hub](#hub)
- [Remote Bridge](#remote-bridge)

---

## Hub

### 1. Plug in the Wyze Sense Bridge

Connect the USB dongle to the Linux host.

### 2. Download the hub package

Download the latest release from the [GitHub Releases page](https://github.com/raetha/wyzesense2mqtt/releases). Replace `X.Y.Z` with the version you want:

```bash
VERSION=X.Y.Z
wget "https://github.com/raetha/wyzesense2mqtt/releases/download/v${VERSION}/ws2m-hub-${VERSION}.tar.gz"
mkdir -p /opt/ws2m/hub
tar -xzf "ws2m-hub-${VERSION}.tar.gz" -C /opt/ws2m/hub/
```

### 3. Install hub dependencies

```bash
pip3 install -r /opt/ws2m/hub/requirements.txt
```

### 4. Create the data directory

```bash
mkdir -p /opt/ws2m/data
```

### 5. Create configuration

Create `/opt/ws2m/data/config.yaml`. You must set at minimum `mqtt_host`:

```yaml
mqtt_host: <your-broker>
mqtt_username: <user>
mqtt_password: <password>
log_level: INFO
```

Alternatively, set configuration via environment variables (`WS2M_MQTT_HOST`, etc.) in the systemd service file.

### 6. Create the systemd service

An example service file is included in the release package. Copy and install it:

```bash
sudo cp /opt/ws2m/hub/ws2m-hub.service.example /etc/systemd/system/ws2m-hub.service
```

Review and edit the installed file if your installation path differs from `/opt/ws2m/hub`.

The service file content:
```ini
[Unit]
Description=WyzeSense2MQTT Hub
Documentation=https://github.com/raetha/wyzesense2mqtt
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/ws2m/hub
ExecStart=/opt/ws2m/hub/service.sh
Restart=always
KillSignal=SIGINT
Environment=WS2M_DATA_DIR=/opt/ws2m/data

[Install]
WantedBy=multi-user.target
```

### 7. Start and enable the service

```bash
sudo systemctl daemon-reload
sudo systemctl start ws2m-hub
sudo systemctl enable ws2m-hub
sudo systemctl status ws2m-hub
```

### 8. View logs

```bash
journalctl -u ws2m-hub -f
```

### 9. Pair sensors

Follow the [pairing instructions](../README.md#pairing-a-sensor) in the main README.

---

## Remote Bridge

Run `ws2m-remote` on the machine that physically holds the WyzeSense USB dongle.

### 1. Plug in the Wyze Sense Bridge

Connect the USB dongle to the remote Linux host.

### 2. Download the remote package

Download the latest release from the [GitHub Releases page](https://github.com/raetha/wyzesense2mqtt/releases). Replace `X.Y.Z` with the version you want:

```bash
VERSION=X.Y.Z
wget "https://github.com/raetha/wyzesense2mqtt/releases/download/v${VERSION}/ws2m-remote-${VERSION}.tar.gz"
mkdir -p /opt/ws2m/remote
tar -xzf "ws2m-remote-${VERSION}.tar.gz" -C /opt/ws2m/remote/
```

### 3. Install remote dependencies

```bash
pip3 install -r /opt/ws2m/remote/requirements.txt
```

### 4. Create the data directory

```bash
mkdir -p /opt/ws2m/remote-data
```

### 5. Create the systemd service

An example service file is included in the release package. Copy and install it:

```bash
sudo cp /opt/ws2m/remote/ws2m-remote.service.example /etc/systemd/system/ws2m-remote.service
```

Review the installed file and uncomment/set `WS2M_HUB_URL` if mDNS discovery is not available on your network.

The service file content:
```ini
[Unit]
Description=WyzeSense2MQTT Remote Bridge
Documentation=https://github.com/raetha/wyzesense2mqtt
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/ws2m/remote
ExecStart=/opt/ws2m/remote/service.sh
Restart=always
KillSignal=SIGINT
Environment=WS2M_DATA_DIR=/opt/ws2m/remote-data
# Uncomment and set if mDNS discovery is not available:
# Environment=WS2M_HUB_URL=ws://192.168.1.10:8765

[Install]
WantedBy=multi-user.target
```

### 6. Start and enable the service

```bash
sudo systemctl daemon-reload
sudo systemctl start ws2m-remote
sudo systemctl enable ws2m-remote
sudo systemctl status ws2m-remote
```

### 7. View logs

```bash
journalctl -u ws2m-remote -f
```

### 8. Adopt the remote

Follow the [adoption instructions](../README.md#adopting-a-remote) in the main README. The hub must have `hub_ws_enabled: true` in its configuration.

