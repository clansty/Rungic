"""Paths owned by standalone installation and removal. No device-side deletion glob."""
PATHS = {
    'RUNGIC_TERMUX_STAGE': '/data/data/com.termux/files/.rungic-stage',
    'RUNGIC_TERMUX_AUDIO': '/data/data/com.termux/files/usr/tmp/rungic-plasma-audio',
    'RUNGIC_PAYLOAD': '/data/adb/rungic-install',
    'RUNGIC_LXC': '/data/adb/rungic-lxc',
    'RUNGIC_CONTROLLER': '/data/adb/rungic-plasma',
    'RUNGIC_CAST': '/data/adb/rungic-wfd',
    'RUNGIC_LEGACY': '/data/adb/rungic-install-legacy',
    'RUNGIC_HOST_STAGE': '/data/adb/.rungic-host-stage',
    'RUNGIC_CAST_STAGE': '/data/adb/.rungic-wfd-stage',
    'RUNGIC_PROVISION': '/data/adb/.rungic-rootfs-provision',
    'RUNGIC_COMPLETE': '/data/adb/rungic-firstboot.complete',
    'RUNGIC_BOOT_LOG': '/data/adb/rungic-firstboot.log',
    'RUNGIC_LAUNCH_LOG': '/data/adb/rungic-install-launch.log',
    'RUNGIC_BOOT_SERVICE': '/data/adb/service.d/00-rungic-firstboot.sh',
    'RUNGIC_RUNTIME_SERVICE': '/data/adb/service.d/rungic-runtime.sh',
    'RUNGIC_RUNTIME_SERVICE_TMP': '/data/adb/service.d/rungic-runtime.sh.tmp',
    'RUNGIC_CAST_SERVICE': '/data/adb/service.d/rungic-cast-watch.sh',
    'RUNGIC_CAST_SERVICE_TMP': '/data/adb/service.d/rungic-cast-watch.sh.new',
    'RUNGIC_CAST_POLICY_TMP': '/data/adb/service.d/rungic-wfd-sepolicy.sh.new',
    'RUNGIC_CAST_POLICY': '/data/adb/service.d/rungic-wfd-sepolicy.sh',
}
APP = 'com.rungic.plasma'
APP_DATA = '/data/user/0/' + APP
HOME = PATHS['RUNGIC_LXC'] + '/runtime/var/lib/lxc/plasma/state/home'
PRESERVED = '/data/adb/rungic-preserved'
COMPAT = '/data/adb/modules/rungic-install-compat'
UNINSTALLED = '/data/adb/rungic-uninstalled'
PENDING = '/data/adb/rungic-uninstalling'
MAINTENANCE_LOCK = '/data/adb/rungic-maintenance.lock'
FIRSTBOOT_LOCK = '/data/adb/rungic-firstboot.lock'
# Lock inodes remain: removing a held lock permits a second owner to create a new inode.
RETAINED = {
    PENDING: 'Hold during incomplete removal. Successful removal clears this marker.',
    '/data/adb/magisk': 'Keep the shared Magisk tools.',
    '/data/adb/magisk/busybox': 'Keep the shared Magisk BusyBox executable.',
    '/data/adb/service.d': 'Keep the shared service directory. Remove only owned service files.',
    COMPAT: 'Block the read-only product seed. Installation can replace this guard.',
    UNINSTALLED: 'Block fallback to the old seed until the installer publishes a new installation.',
    MAINTENANCE_LOCK: 'Serialize installation and removal.',
    FIRSTBOOT_LOCK: 'Serialize removal with the existing first-boot worker.',
    '/data/adb/rungic-install.lock': 'Keep the installation lock inode.',
    '/data/adb/rungic-cast-install.lock': 'Keep the casting lock inode.',
    PRESERVED: 'Keep historical preserved homes and their preservation records.',
    '/storage/emulated/0/Plasma': 'Android user files are outside removal scope.',
    '/product/app/Rungic/Rungic.apk': 'Read-only Android base APK.',
    '/product/etc/rungic': 'Read-only Android base seed.',
    '/data/data/com.termux': 'Keep Termux, its prefix and unrelated user files. Remove only the two Rungic private paths in the deletion plan.',
}


def staging_path(release):
    # Caller validates release before it reaches a shell.
    return '/data/local/tmp/rungic-' + release
