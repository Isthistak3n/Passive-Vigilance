#!/bin/bash
logger "Passive Vigilance: setting wlan1 to monitor mode"
ip link set wlan1 down
iw wlan1 set monitor none
ip link set wlan1 up
logger "Passive Vigilance: wlan1 monitor mode active"
