#!/usr/bin/env python
# Borrowed from https://github.com/HclX/WyzeSensePy/blob/master/sample.py with slight modifications

"""Example of using WyzeSense USB bridge tool

**Usage:** ::
  bridge_tool_cli.py [options]

**Options:**

    -d, --debug     output debug log messages to stderr
    -v, --verbose   print and log more information
    --device PATH   USB device path [default: /dev/hidraw0]

**Examples:** ::

  bridge_tool_cli.py --device /dev/hidraw0

"""
from __future__ import print_function

from builtins import input

import re
import sys
import logging
import binascii
import wyzesense
from datetime import datetime

def on_event(ws, e):
    s = f"[{datetime.fromtimestamp(e.timestamp).strftime('%Y-%m-%d %H:%M:%S')}][{e.mac}]: {e}"
    print(s)

def main(args):
    if args['--debug']:
        loglevel = logging.DEBUG - (1 if args['--verbose'] else 0)
        logging.getLogger("wyzesense").setLevel(loglevel)
        logging.getLogger().setLevel(loglevel)

    device = args['--device']
    print(f"Opening wyzesense gateway [{device}]")
    try:
        ws = wyzesense.Open(device, on_event, logging.getLogger())
        if not ws:
            print("Open wyzesense gateway failed")
            return 1
        print("Gateway info:")
        print(f"\tMAC:{ws.MAC}")
        print(f"\tVER:{ws.Version}")
        print(f"\tENR:{binascii.hexlify(ws.ENR)}")
    except IOError:
        print(f"No device found on path {device}")
        return 2

    def List(unused_args):
        result = ws.List()
        print(f"{len(result)} sensors paired:")
        logging.debug(f"{len(result)} sensors paired:")
        for mac in result:
            print(f"\tSensor: {mac}")
            logging.debug(f"\tSensor: {mac}")

    def Pair(unused_args):
        result = ws.Scan()
        (s_mac, s_type, s_version) = result
        if result:
            print(f"Sensor found: mac={s_mac}, type={s_type}, version={s_version}")
            logging.debug(f"Sensor found: mac={s_mac}, type={s_type}, version={s_version}")
        else:
            print("No sensor found!")
            logging.debug("No sensor found!")

    def Unpair(mac_list):
        for mac in mac_list:
            if len(mac) != 8:
                print(f"Invalid mac address, must be 8 characters: {mac}")
                logging.debug(f"Invalid mac address, must be 8 characters: {mac}")
                continue
            print(f"Un-pairing sensor {mac}:")
            logging.debug(f"Un-pairing sensor {mac}:")
            result = ws.Delete(mac)
            if result is not None:
                print(f"Result: {result}")
                logging.debug(f"Result: {result}")
            print(f"Sensor {mac} removed")
            logging.debug(f"Sensor {mac} removed")

    def Fix(unused_args):
        invalid_mac_list = [
            "00000000",
            "\0\0\0\0\0\0\0\0",
            "\x00\x00\x00\x00\x00\x00\x00\x00"
        ]
        print("Un-pairing bad sensors")
        logging.debug("Un-pairing bad sensors")
        for mac in invalid_mac_list:
            result = ws.Delete(mac)
            if result is not None:
                print(f"Result: {result}")
                logging.debug(f"Result: {result}")
        print("Bad sensors removed")
        logging.debug("Bad sensors removed")

    def Chime(args):
        if len(args) < 4:
            print("Need 4 parameters")
            return

        mac, ring, repeat, volume = args
        ws.PlayChime(mac, int(ring), int(repeat), int(volume))

    def Raw(args):
        if len(args) <= 0:
            print("Missing argument!")
            return

        data = args[0]
        data = bytes([int(x, 16) for x in data.strip().split(',')])
        str_data = ','.join([f"{x:02X}" for x in data])
        print(f"Sending raw bytes: {str_data}")
        ws.SendRaw(data)

    def HandleCmd():
        cmd_handlers = {
            'L': ('L - [L]ist paired sensors', List),
            'P': ('P - [P]air new sensors', Pair),
            'U': ('U - [U]npair sensor, args: <mac>', Unpair),
            'F': ('F - [F]ix invalid sensors', Fix),
            'C': ('C - Play [C]hime, args: <mac> <ring> <repeat> <volume>', Chime),
            'R': ('R - Sending [R]aw packet, args: <hex bytes, separated by comma', Raw),
            'X': ('X - E[X]it tool', None),
        }

        for v in list(cmd_handlers.values()):
            print(v[0])

        cmd_and_args = input("Action:").strip().upper().split()
        if len(cmd_and_args) == 0:
            return True

        cmd = cmd_and_args[0]
        if cmd not in cmd_handlers:
            return True

        handler = cmd_handlers[cmd]
        if not handler[1]:
            return False

        print("------------------------")
        handler[1](cmd_and_args[1:])
        print("------------------------")
        return True

    try:
        while HandleCmd():
            pass
    finally:
        ws.Stop()

    return 0


if __name__ == '__main__':
    logging.basicConfig(format='%(levelname)s %(asctime)s %(message)s')

    try:
        from docopt import docopt
    except ImportError:
        sys.exit("the 'docopt' module is needed to execute this program")

    # remove restructured text formatting before input to docopt
    usage = re.sub(r'(?<=\n)\*\*(\w+:)\*\*.*\n', r'\1', __doc__)
    sys.exit(main(docopt(usage)))
