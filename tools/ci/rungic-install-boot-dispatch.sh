#!/system/bin/sh
# Systemless compatibility with already-flashed product seed launchers.
set -eu
[ ! -e /data/adb/rungic-uninstalling ] || { echo 'Rungic 卸载未完成，请重新运行卸载。' >&2; exit 1; }
if [ -f /data/adb/rungic-install/active.env ]; then
    exec /system/bin/sh /data/adb/rungic-install/payload/firstboot.sh /data/adb/rungic-install/payload
fi
[ ! -e /data/adb/rungic-uninstalled ] || exit 0
exec /system/bin/sh /data/adb/rungic-install-legacy/firstboot.sh
