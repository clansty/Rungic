#!/system/bin/sh
# Magisk late_start service, asynchronous; never runs the display Activity.
[ ! -x /data/adb/rungic-plasma/rungic-runtime ] || exec /data/adb/rungic-plasma/rungic-runtime boot
