import paho.mqtt.client as mqtt
import json
from wyzesense_custom import *

import sys
sys.stdout = open("wyze-mqtt.log","w")

diff = lambda l1, l2: [x for x in l1 if x not in l2]

def on_connect(client, userdata, flags, rc):
    print("Connected with result code " + str(rc))
    client.message_callback_add(config["subscribeScanTopic"], on_message_scan)
    client.message_callback_add(config["subscribeRemoveTopic"], on_message_remove)
    print("Handlers Added")

def on_disconnect(client, userdata, rc):
    print("Disconnected with result code " + str(rc))
    client.message_callback_remove(config["subscribeScanTopic"])
    client.message_callback_remove(config["subscribeRemoveTopic"])
    print("Handlers Removed")

def on_message(client, userdata, msg):
    print(msg.topic+" " + str(msg.payload))  

def on_message_scan(client, userdata, msg):
    print("In on_message_scan: {0}".format(msg.payload.decode()))
    prescan_result = ws.List()
    print("Result of prescan {0}".format(prescan_result))   

    ws.Scan()
    postscan_result = ws.List()
    print("Result of postscan {0}".format(postscan_result))  

    s = diff(postscan_result, prescan_result)  
    print("Diff is {0}".format(s))  

    if s != []:
        jsonData = json.dumps({"macs": s})
        topic = "home/wyze/newdevice" 
        print(jsonData)
        client.publish(topic , jsonData)
    else:
        print("Empty Scan")

def on_message_remove(client, userdata, msg):
    print(msg.topic+" " + str(msg.payload))          

def read_config():
    with open("config.json") as config_file:
        data = json.load(config_file)
    return data

config = read_config()
client = mqtt.Client(config["mqtt"]["client"])
client.username_pw_set(username=config["mqtt"]["user"], password=config["mqtt"]["password"])
client.connect(config["mqtt"]["host"], config["mqtt"]["port"], 60)    

client.subscribe([(config["subscribeScanTopic"], 1), (config["subscribeRemoveTopic"],1)])
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message


def on_event(ws, event):
    if event.Type == 'state':
        (sensor_type, sensor_state, sensor_battery, sensor_signal) = event.Data
        data = {
            "available": True,
            "mac": event.MAC,
            "state": 1 if sensor_state == "open" or sensor_state == "active" else 0,
            "device class": "device motion" if sensor_type == "motion" else "device door",
            "device class timestamp": event.Timestamp.isoformat(),
            "rsssi": sensor_signal * -1,
            "battery level": sensor_battery
        }

        jsonData = json.dumps(data)
        topic = config["publishTopic"]+"{0}".format(event.MAC)
        print(data)
        client.publish(topic , jsonData)

def beginConn():
    return Open(config["usb"], on_event)

#Connect to USB
ws = beginConn()

# Message Loop
client.loop_forever()