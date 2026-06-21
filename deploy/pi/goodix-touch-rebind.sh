#!/bin/bash
# Touch Display 2 = ILI9881C DSI panel + Goodix GT911 touch controller, with the
# touch IC powered from the panel rail. At boot the Goodix I2C driver probes
# (~5.5s) right around when the panel finishes binding (~6.0s); when it loses
# that race it dies with "Goodix-TS ...: I2C communication failure: -5" and
# never retries, so the touchscreen never enumerates as an input device.
#
# Once the panel is powered (the console fbcon has modeset it, which has
# certainly happened by the time this service runs) a manual driver bind
# succeeds. Retry the bind until the input node shows up, then exit.
#
# Installed to /usr/local/bin and run by goodix-touch-rebind.service, ordered
# Before=usage-dashboard-gui.service so touch is present before X grabs devices.
set -u
DRV=/sys/bus/i2c/drivers/Goodix-TS

for _ in $(seq 1 30); do
  grep -qi goodix /proc/bus/input/devices && exit 0
  # The Goodix sits at I2C address 0x5d; the bus number isn't fixed, so glob it.
  DEV=$(basename "$(ls -d /sys/bus/i2c/devices/*-005d 2>/dev/null | head -1)" 2>/dev/null)
  if [ -n "$DEV" ] && [ ! -e "$DRV/$DEV" ]; then
    echo "$DEV" > "$DRV/bind" 2>/dev/null || true
  fi
  sleep 1
done
exit 0
