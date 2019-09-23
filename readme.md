# Wyze sense Mqtt wrapper
A wrapper for the excellent Kevin Vincent (https://github.com/kevinvincent/ha-wyzesense) wyze sense library.Kevins library needs the wyze sense hub to be plugged into a USB port where Home Assistant is running. All my USB ports were taken so I wrote this little wrapper that allows me to run the wyze hub on another machine and communicate with Home Assistant via MQTT. 

## Setup

I have included a simple unit file so that the wrapper can be setup to run as a systemd service. Edit the files as required.

* `sudo cp wyze-mqtt.service /etc/systemd/system/`
* Edit the `service` file to suit your requirements
* `sudo systemctl daemon-reload`
* `sudo systemctl start wyze-mqtt`
* `sudo systemctl status wyze-mqtt`

### Config file

The wrapper relies on a config file to be present in the same directory. The config file has to be called `config.json`. A sample is included below:

```json
{
    "mqtt":{
        "host": "Mqtt host",
        "port": "Mqtt port",
        "user": "Mqtt username",
        "password": "Mqtt password",
        "client": "Mqtt client name"
    },
    "publishTopic":"/wyze/",
    "publishScanResult":"home/wyze/newdevice",
    "subscribeScanTopic":"/wyze/scan",
    "subscribeRemoveTopic": "/wyze/remove",
    "usb":"/dev/hidraw0"
}
``` 
### Home Assistant

To add sensors to the `wyze hub`, you need to publish a message from Home Assistant that will trigger the scan on the `wyze-mqtt` wrapper. This is done by publishing to topic defined under `subscribeScanTopic`. If successful the wrapper will respond with the newly added sensors MAC address. Add the following automation in home assisant to receive the newly added MAC addresses
```yaml
- alias: Listen for new wyze sense sensors added to hub.
  initial_state: true
  trigger:
    - platform: mqtt
      topic: 'home/wyze/newdevice'
  action:
    - service: persistent_notification.create
      data_template:
        title: Wyze Sense Sensors Discovery
        message: "{{ trigger.payload}}"
```

Some sample configurations in Home Assistant. One is for a door / window sensor and the other is for a motion sensor.
```yaml
binary_sensor:
  - platform: mqtt
    name: A Window
    state_topic: "home/wyze/123456789"
    device_class: "window"
    value_template: "{{ value_json.state }}" 
    payload_on: 1
    payload_off: 0

sensor:
  - platform: mqtt
    name: A Motion
    state_topic: "home/wyze/987654321"
    value_template: "{{ value_json.state }}"  
    force_update: true  
```

## Todo
* Add support for removing sensors.
* Proper logging
* Mqtt sometimes gets a `connection timeout` and does not reconnect.

## Tested

Tested on Windows 10 running Ubuntu in a virtual machine.