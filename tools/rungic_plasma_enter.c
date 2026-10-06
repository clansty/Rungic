/* Launch the Plasma LXC control environment in a private mount namespace.
 * Run as Magisk root. The container's own isolation is created by liblxc.
 * Nothing is mounted in Android's original mount namespace.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define ROOT "/data/adb/rungic-lxc/runtime"

static _Noreturn void fail(const char *what) {
    perror(what);
    exit(1);
}

int main(int argc, char **argv) {
    if (argc < 2 || geteuid() != 0) {
        fprintf(stderr, "Usage (root): rungic-plasma-enter /absolute/program [args...]\n");
        return 2;
    }
    if (argv[1][0] != '/') {
        fprintf(stderr, "Program must be an absolute path inside the runtime.\n");
        return 2;
    }
    if (unshare(CLONE_NEWNS) < 0) fail("unshare mount namespace");
    if (mount(NULL, "/", NULL, MS_PRIVATE | MS_REC, NULL) < 0)
        fail("make mounts private");
    if (mount(ROOT, ROOT, NULL, MS_BIND, NULL) < 0) fail("bind runtime");
    const char *names[] = {"proc", "sys", "dev"};
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        char source[32], target[256];
        snprintf(source, sizeof(source), "/%s", names[i]);
        snprintf(target, sizeof(target), ROOT "/%s", names[i]);
        if (mount(source, target, NULL, MS_BIND | MS_REC, NULL) < 0)
            fail(target);
    }
    /* State queries must work before any display/audio client has created its
     * bind sources. They only read LXC state and never attach or start payloads. */
    if (strcmp(argv[1], "/usr/bin/lxc-info") != 0) {
        if (mount("/storage/emulated/0/Plasma", ROOT "/mnt/plasma-shared", NULL,
                  MS_BIND, NULL) < 0) fail("bind shared Linux files");
        if (mkdir(ROOT "/mnt/plasma-wayland", 0755) < 0 && errno != EEXIST)
            fail("mkdir Wayland bridge");
        if (mount("/data/user/0/com.rungic.plasma/files/tmp", ROOT "/mnt/plasma-wayland", NULL,
                  MS_BIND, NULL) < 0) fail("bind Android Wayland socket directory");
        if (mkdir(ROOT "/mnt/plasma-audio", 0755) < 0 && errno != EEXIST)
            fail("mkdir audio bridge");
        if (mount("/data/data/com.termux/files/usr/tmp/rungic-plasma-audio",
                  ROOT "/mnt/plasma-audio", NULL, MS_BIND, NULL) < 0)
            fail("bind Android audio socket directory");
    }
    /* A plain chroot leaves the mount namespace rooted at Android. LXC's
     * pidfd-based attach then resets / to Android despite joining the correct
     * namespace. Make the control environment the namespace's actual root.
     */
    if (chdir(ROOT) < 0) fail("chdir runtime");
    if (mkdir(".oldroot", 0700) < 0 && errno != EEXIST) fail("mkdir oldroot");
    if (syscall(SYS_pivot_root, ".", ".oldroot") < 0) fail("pivot runtime");
    if (chdir("/") < 0) fail("chdir root");
    if (umount2("/.oldroot", MNT_DETACH) < 0) fail("detach old Android root");
    if (clearenv() != 0 ||
        setenv("PATH", "/usr/sbin:/usr/bin:/sbin:/bin", 1) != 0 ||
        setenv("HOME", "/root", 1) != 0 ||
        setenv("TERM", "xterm-256color", 1) != 0)
        fail("environment");
    execv(argv[1], argv + 1);
    fail("exec runtime program");
}
