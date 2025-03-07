# Linux Systemd Installation

The gateway can also be run as a systemd service for those not wanting to use Docker. Requires Python 3.6 or newer. You may need to do all commands as root, depending on filesystem permissions. This is NOT actively tested, please submit an issue or PR if you experience problems.
1. Plug the Wyze Sense Bridge into a USB port on the Linux host.
2. Pull down a copy of the repository
```bash
cd /tmp
git clone https://github.com/raetha/wyzesense2mqtt.git
```
3. Create local application folders (Select a location that works for you, example uses /opt/wyzesense2mqtt)
```bash
mv /tmp/wyzesense2mqtt/wyzesense2mqtt /opt/wyzesense2mqtt
rm -rf /tmp/wyzesense2mqtt
cd /opt/wyzesense2mqtt
mkdir config
mkdir logs
```
4. Prepare config.yaml file. You must set MQTT host parameters! Username and password can be blank if unused. (see example below)
```bash
cp samples/config.yaml config/config.yaml
vim config/config.yaml
```
5. Modify logging.yaml file if desired (optional)
```bash
cp samples/logging.yaml config/logging.yaml
vim config/logging.yaml
```
6. If desired, pre-populate a sensors.yaml file with your existing sensors. This file will automatically be created if it doesn't exist. (see example below) (optional)
```bash
cp samples/sensors.yaml config/sensors.yaml
vim config/sensors.yaml
```
7. Install dependencies
```bash
sudo pip3 install -r requirements.txt
```
8. Configure the service
```bash
vim wyzesense2mqtt.service # Only modify if not using default application path
sudo cp wyzesense2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start wyzesense2mqtt
sudo systemctl status wyzesense2mqtt
sudo systemctl enable wyzesense2mqtt # Enable start on reboot
```
9. Pair sensors following the instructions at [Paring a Sensor](/readme.md#pairing-a-sensor). You do NOT need to re-pair sensors that were already paired, they should be found automatically on start and added to the config file with default values, though the sensor version will be unknown and the class will default to opening, i.e. a contact sensor. You should manually update these entries.
