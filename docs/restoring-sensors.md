Provided by exeljb - https://forums.wyze.com/t/unbricking-wyze-contact-sensor-pcb-reset-pin/146856/101
Wyzeback - https://github.com/sycophantic/wyzeback

## Restoring Wyze Door Contact Sensors MAC Addresses

# Parts Needed:
* Uses UniFlash 6 (search for NWJS.app on Macs)
* CC1310 Launchpad Board w/ microUSB cable
* Modified .bin file that contains matching MAC address ( see below)
* Jumper Cable with 2 male pins
* 2 mini-grabbers to female pin
* Wyze Contact sensor removed from case
* There’s a small white plastic melt that needs to be removed, it’s near the antenna/center of sensor

# How to handle wyzeback on machines where the script doesn’t correctly create a valid bin:
1. Locate the original wyzesense_door_AABBCCDD.bin file and open it in BBedit
2. Do a Find and Replace, finding AABBCCDD and replacing with whatever the MAC address should be for the corresponding sensor
3. Save the file as wyzesense_door_<new_mac_address>.bin denoting whatever the sensors original MAC address was OR rename with description of sensor location (ie front_door.bin)

# Burning the firmware
1. Use some double sided foam tape to secure the sensor down to the working surface. Avoid metal surfaces!
2. Ensure magnet is making up the contact sensor (possibly puts sensor in fw load mode)!
3. Connect the sensor to the LaunchPad like so
* GND goes to flat pad for the battery, ensure the mini grabber does not also contact the pcb via’s located right under the batter contact. Carefully hold the ring part of the battery contac
t while gentling prying the battery contact finger up
* 3.3V from LaunchPad goes to the tall part of the battery holder
* TMS from LaunchPad goes to the M pad on the sensor board.
* TCK from LaunchPad goes to the C pad on the sensor board
* NOTE: The correct TMS and TCK are located in the center of the launchpad. Remove the black jumpers for both. If you use the TMS and TCK that are on the side of the launchpad, loading will 
not occur!

<insert image>

1. Ensure that the pins touching the M and C pad have enough pressure and are aligned correctly for adequate contact with the 2 pads. It’s also possible that the pin for pad M can contact th
e 3.3 volts of the batter holder which will result in a failed load.
2. Try to keep movement to a minimum while the pins are touching the pads

<insert image>

3. Open UniFlash and Connect the USB cable
4. Click Browse and navigate to where ever you saved your new .bin files, then select the .bin that has the correct MAC address for the sensor being loaded
5. Select the Settings & Utilities tab on the left and click Erase Entire Flash
6. Select the Program tab on the left, Click the Load Image and wait for the success.

