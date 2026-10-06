#!/usr/bin/python3
"""Demand-driven Android mic / camera / phone output integration. Run as the desktop user.

PulseAudio remains the audio server; PipeWire exports Camera Video/Source nodes.
The default sink follows Android's routing (a cast screen while casting); the
android_phone sink always plays on the phone itself (docs/59). "Linux Speaker"
and "Linux Microphone" are virtual devices for software that listens and speaks in
place of a person, e.g. an assistant taking part in a call (docs/62).
Capture sockets are private to the Android app and never listen on TCP.
"""
import fcntl
import gettext
import json
import logging
import os
import select
import signal
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

import rungic_host_watch

# Device names in the session's language: po/zh_CN/rungic-shared.po, installed by rungic-plasma-bridges.
_ = gettext.translation('rungic-shared', fallback=True).gettext

RUNTIME = Path(os.environ['XDG_RUNTIME_DIR'])
# The capture socket: the media backend's (MediaDaemon, docs/117), else the app's own (an app from
# before it); another one only for a stand-in (tools/system/tests).
CAPTURE_SOCKETS = ('/var/lib/rungic-host/media/capture.sock', '/mnt/android-wayland/capture.sock')


def capture_socket():
    if os.environ.get('RUNGIC_CAPTURE_SOCKET'):
        return os.environ['RUNGIC_CAPTURE_SOCKET']
    return next((path for path in CAPTURE_SOCKETS if os.path.exists(path)), CAPTURE_SOCKETS[0])


FIFO = RUNTIME / 'rungic-microphone.pcm'
SOURCE = 'android_microphone'
PHONE_FIFO = RUNTIME / 'rungic-phone-output.pcm'
PHONE_SINK = 'android_phone'
# Virtual devices (docs/62). A program that should hear an application plays
# nothing itself: the application outputs to linux_speaker and the listener
# records linux_speaker.monitor. Audio played into linux_microphone_input comes
# out of linux_microphone, which any application can choose as its microphone.
LINUX_SPEAKER = 'linux_speaker'
LINUX_MIC = 'linux_microphone'
LINUX_MIC_INPUT = 'linux_microphone_input'
PHONE_HEADER = {'ok': True, 'rate': 48000, 'channels': 2, 'format': 's16le'}
F_SETPIPE_SZ = 1031
LOG = logging.getLogger('android-media')
STOP = threading.Event()
CHANGED = threading.Event()      # the audio server reported a device or stream change
HOST_CHANGED = threading.Event()  # the app reported a change of what it shows or permits (capture)
WAKE = threading.Event()
RECHECK = 30   # seconds between checks of the audio server without an event


def described(key, name):
    """A module's properties argument naming the device. A description with a space needs the
    whole property list quoted for the module argument parser ('...="Linux Speaker"'); without
    it loading fails. Quotes inside a translated name would break that quoting."""
    name = name.replace('"', '').replace("'", '')
    return f'{key}=\'device.description="{name}"\''


def pactl(*args):
    return subprocess.check_output(['pactl', *args], text=True, timeout=3,
                                   env={**os.environ, 'LC_ALL': 'C'}).strip()


def read_header(client):
    data = bytearray()
    while len(data) < 65536:
        c = client.recv(1)
        if not c:
            raise OSError('Android capture connection closed')
        if c == b'\n':
            result = json.loads(data)
            if 'error' in result:
                raise OSError(result['error'])
            return result
        data.extend(c)
    raise OSError('Android response too large')


def host_info():
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(os.environ.get('RUNGIC_PLATFORM_SOCKET', '/mnt/android-wayland/platform.sock'))
        client.sendall(b'{"op":"capture-info"}\n')
        return read_header(client)


class Microphone:
    def __init__(self):
        self.thread = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.socket = None
        self.retry_at = 0

    def stop(self):
        self.cancel.set()
        with self.lock:
            if self.socket:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self.thread:
            self.thread.join(3)
            if not self.thread.is_alive():
                self.thread = None

    def set_wanted(self, wanted):
        if not wanted:
            self.stop()
        elif (not self.thread or not self.thread.is_alive()) and time.monotonic() >= self.retry_at:
            self.cancel.clear()
            self.thread = threading.Thread(target=self.capture, daemon=True)
            self.thread.start()

    def capture(self):
        audio_priority()
        fd = None
        try:
            with socket.socket(socket.AF_UNIX) as client:
                with self.lock:
                    self.socket = client
                if self.cancel.is_set():
                    return
                client.settimeout(50)  # Android's interactive runtime permission dialog.
                client.connect(capture_socket())
                client.sendall(b'{"op":"microphone"}\n')
                header = read_header(client)
                if header != {'ok': True, 'rate': 48000, 'channels': 1, 'format': 's16le'}:
                    raise OSError('Unsupported Android microphone format')
                client.settimeout(3)
                fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                LOG.info('microphone consumer started')
                while not self.cancel.is_set() and not STOP.is_set():
                    block = memoryview(client.recv(9600))
                    if not block:
                        raise OSError('Android microphone stream closed')
                    while block and not self.cancel.is_set():
                        if select.select([], [fd], [], 0.2)[1]:
                            try:
                                count = os.write(fd, block)
                                block = block[count:]
                            except BlockingIOError:
                                pass
        except (OSError, ValueError) as error:
            if not self.cancel.is_set():
                LOG.warning('microphone: %s', error)
                self.retry_at = time.monotonic() + 5
        finally:
            with self.lock:
                self.socket = None
            if fd is not None:
                os.close(fd)
            # Discard at most one FIFO of pending audio between consumers.
            try:
                drain = os.open(FIFO, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                try:
                    while os.read(drain, 65536):
                        pass
                except BlockingIOError:
                    pass
                os.close(drain)
            except OSError:
                pass
            LOG.info('microphone consumer stopped')


AUDIO_NICE = -11   # PulseAudio's own level; the session allows it (RLIMIT_NICE), not realtime


def audio_priority():
    """Audio threads forward in small blocks: under load (a call, the desktop rendering) a
    normal-priority thread was starved and the phone heard the call in pieces (docs/63)."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), AUDIO_NICE)
    except OSError as error:
        LOG.warning('audio thread priority: %s', error)


class PhoneOutput:
    """Forward the android_phone sink to the app's AudioTrack while the sink is open.

    The sink runs on the system clock (use_system_clock_for_timing): PulseAudio writes at a steady
    rate and this drains the FIFO as it comes. Paced by this reader instead (until 2026-10-02), the
    sink wrote in bursts behind a blocking 150 ms AudioTrack: a game's sound stuttered, about half a
    second late. The FIFO and socket buffers are small (about 40 ms each); the app's AudioTrack keeps
    its own fill bounded (CaptureBridge.phoneOutput).
    """

    def __init__(self):
        self.thread = None
        self.cancel = threading.Event()
        self.retry_at = 0

    def stop(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(3)
            if not self.thread.is_alive():
                self.thread = None

    def set_wanted(self, wanted):
        if not wanted:
            self.stop()
        elif (not self.thread or not self.thread.is_alive()) and time.monotonic() >= self.retry_at:
            self.cancel.clear()
            self.thread = threading.Thread(target=self.play, daemon=True)
            self.thread.start()

    def play(self):
        audio_priority()
        fd = None
        try:
            fd = os.open(PHONE_FIFO, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                fcntl.fcntl(fd, F_SETPIPE_SZ, 8192)
            except OSError:
                pass
            with socket.socket(socket.AF_UNIX) as client:
                client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
                client.settimeout(3)
                client.connect(capture_socket())
                client.sendall(b'{"op":"phone-output"}\n')
                if read_header(client) != PHONE_HEADER:
                    raise OSError('Unsupported phone output format')
                LOG.info('phone output started')
                while not self.cancel.is_set() and not STOP.is_set():
                    if not select.select([fd], [], [], 0.2)[0]:
                        continue
                    try:
                        block = os.read(fd, 1920)   # 10 ms
                    except BlockingIOError:
                        continue
                    if block:
                        client.sendall(block)
        except (OSError, ValueError) as error:
            if not self.cancel.is_set():
                LOG.warning('phone output: %s', error)
                self.retry_at = time.monotonic() + 5
        finally:
            if fd is not None:
                # The sink is suspended now; drop what it left in the FIFO.
                try:
                    while os.read(fd, 65536):
                        pass
                except OSError:
                    pass
                os.close(fd)
            LOG.info('phone output stopped')


class Cameras:
    def __init__(self):
        self.children = {}
        self.retry_at = {}

    def update(self, info):
        available = info.get('visible') and not (info.get('cameraDenied') and not info.get('cameraPermission'))
        desired = {str(c['id']): c for c in info.get('cameras', [])} if available else {}
        for key, process in list(self.children.items()):
            if key not in desired or process.poll() is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                del self.children[key]
                self.retry_at[key] = time.monotonic() + 2
        for key, metadata in desired.items():
            if key not in self.children and time.monotonic() >= self.retry_at.get(key, 0):
                args = ['/usr/bin/rungic-camera-source', key]
                args.extend(str(metadata[k]) for k in ('width', 'height', 'rotation', 'facing'))
                self.children[key] = subprocess.Popen(args)
                LOG.info('camera %s available (%s)', key, metadata['facing'])


def source_index():
    # Short lists only: PA 17's JSON exporter rejects UTF-8 descriptions,
    # such as the phone sink's monitor source.
    for line in pactl('list', 'short', 'sources').splitlines():
        fields = line.split('\t')
        if len(fields) >= 2 and fields[1] == SOURCE:
            return int(fields[0])
    return None


def ensure_source():
    index = source_index()
    if index is None:
        if FIFO.exists() and not stat.S_ISFIFO(FIFO.stat().st_mode):
            raise OSError('Microphone path is not a FIFO')
        module = pactl('load-module', 'module-pipe-source', 'source_name=' + SOURCE,
                      'file=' + str(FIFO), 'format=s16le', 'rate=48000', 'channels=1',
                      'channel_map=mono', 'source_properties=device.description=AndroidMicrophone')
        os.chmod(FIFO, 0o600)
        pactl('set-default-source', SOURCE)
        LOG.info('microphone source ready (module %s)', module)
        index = source_index()
    return index


def ensure_phone_sink():
    """Return the android_phone sink state (RUNNING, IDLE or SUSPENDED)."""
    for line in pactl('list', 'short', 'sinks').splitlines():
        fields = line.split('\t')
        if len(fields) >= 5 and fields[1] == PHONE_SINK:
            return fields[4]
    if PHONE_FIFO.exists() and not stat.S_ISFIFO(PHONE_FIFO.stat().st_mode):
        raise OSError('Phone output path is not a FIFO')
    module = pactl('load-module', 'module-pipe-sink', 'sink_name=' + PHONE_SINK,
                   'file=' + str(PHONE_FIFO), 'format=s16le', 'rate=48000', 'channels=2',
                   'use_system_clock_for_timing=yes',
                   described('sink_properties', _('This Phone')))
    os.chmod(PHONE_FIFO, 0o600)
    LOG.info('phone output sink ready (module %s)', module)
    return 'IDLE'


def ensure_linux_devices():
    """Create the virtual devices once; they must never become the default devices.

    Only after the Android output (the tunnel, which appears asynchronously) is
    there: the default is settled then, and a default taken by a new virtual
    device is handed back to the Android output and microphone."""
    sinks = [line.split('\t')[1] for line in pactl('list', 'short', 'sinks').splitlines() if '\t' in line]
    sources = [line.split('\t')[1] for line in pactl('list', 'short', 'sources').splitlines() if '\t' in line]
    if 'android' not in sinks or (LINUX_SPEAKER in sinks and LINUX_MIC_INPUT in sinks and LINUX_MIC in sources):
        return
    if LINUX_SPEAKER not in sinks:
        pactl('load-module', 'module-null-sink', 'sink_name=' + LINUX_SPEAKER, 'rate=48000', 'channels=2',
              described('sink_properties', _('Linux Speaker')))
    if LINUX_MIC_INPUT not in sinks:
        pactl('load-module', 'module-null-sink', 'sink_name=' + LINUX_MIC_INPUT, 'rate=48000', 'channels=1',
              'channel_map=mono', described('sink_properties', _('Linux Microphone Input')))
    if LINUX_MIC not in sources:
        pactl('load-module', 'module-remap-source', 'master=' + LINUX_MIC_INPUT + '.monitor',
              'source_name=' + LINUX_MIC, 'rate=48000', 'channels=1', 'channel_map=mono',
              described('source_properties', _('Linux Microphone')))
    ours = (LINUX_SPEAKER, LINUX_MIC_INPUT, LINUX_MIC, LINUX_SPEAKER + '.monitor', LINUX_MIC_INPUT + '.monitor')
    if pactl('get-default-sink') in ours:
        pactl('set-default-sink', 'android')
    if pactl('get-default-source') in ours and SOURCE in sources:
        pactl('set-default-source', SOURCE)
    LOG.info('Linux speaker and microphone ready')


def watch_pulse():
    """Wake the main loop on the audio server's device and stream events. The C locale: the
    session's zh_CN made pactl print "事件…于 source", and no event ever matched."""
    while not STOP.is_set():
        try:
            process = subprocess.Popen(['pactl', 'subscribe'], stdout=subprocess.PIPE, text=True,
                                       env={**os.environ, 'LC_ALL': 'C'})
            CHANGED.set()   # (re)connected: the server may have restarted without our modules
            WAKE.set()
            while not STOP.is_set() and process.poll() is None:
                if select.select([process.stdout], [], [], 1)[0]:
                    line = process.stdout.readline()
                    if not line:
                        break
                    if ' on source' in line or ' on sink ' in line or ' on server ' in line:
                        CHANGED.set()
                        WAKE.set()
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
        STOP.wait(2)


def stop(*_):
    STOP.set()
    WAKE.set()


def host_changed():
    HOST_CHANGED.set()
    WAKE.set()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    lock = open(RUNTIME / 'rungic-media.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    microphone, cameras, phone = Microphone(), Cameras(), PhoneOutput()
    threading.Thread(target=watch_pulse, daemon=True).start()
    # The app's capture state (in front, permissions, cameras) when it changes, not every second.
    rungic_host_watch.watch(('capture',), host_changed, legacy=1, name='capture-watch')
    info = {}
    linux_devices_error = None
    # The audio server is asked (each pactl is a client, and a burst of events for every
    # listener) only after one of its device or stream events, and every RECHECK seconds
    # in case one was missed. Polling it twice a second cost ~15 % of a core at idle.
    recheck_at = 0
    recording = False       # a stream records from the Android microphone
    phone_state = 'SUSPENDED'
    try:
        while not STOP.is_set():
            now = time.monotonic()
            if HOST_CHANGED.is_set():
                HOST_CHANGED.clear()
                try:
                    info = host_info()
                except (OSError, ValueError):
                    info = {}
            cameras.update(info)   # also restarts a camera source that exited
            if CHANGED.is_set() or now >= recheck_at:
                CHANGED.clear()
                recheck_at = now + RECHECK
                try:
                    source = ensure_source()
                    try:
                        ensure_linux_devices()
                    except (OSError, subprocess.SubprocessError) as error:
                        # The virtual devices are optional: never stop the microphone
                        # and phone output over them.
                        if linux_devices_error != str(error):
                            LOG.warning('Linux speaker/microphone not available: %s', error)
                        linux_devices_error = str(error)
                    # PA 17's JSON exporter rejects UTF-8 application names. Only
                    # numeric Source and yes/no Corked fields are needed here.
                    outputs = pactl('list', 'source-outputs').split('Source Output #')[1:]
                    recording = False
                    for block in outputs:
                        fields = dict(line.strip().split(': ', 1) for line in block.splitlines() if ': ' in line)
                        if fields.get('Source') == str(source) and fields.get('Corked') == 'no':
                            recording = True
                    phone_state = ensure_phone_sink()
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    recording, phone_state = False, 'SUSPENDED'
                    LOG.warning('audio server unavailable: %s', error)
                    recheck_at = now + 3
            permission = not (info.get('microphoneDenied') and not info.get('microphonePermission'))
            microphone.set_wanted(bool(recording and info.get('visible') and permission and not (info.get('communication') or {}).get('active')))
            phone.set_wanted(phone_state != 'SUSPENDED')
            # Cameras and the microphone retry a failed start after a few seconds.
            WAKE.wait(max(0.05, min(2, recheck_at - time.monotonic())))
            WAKE.clear()
    finally:
        microphone.stop()
        phone.stop()
        cameras.update({})


if __name__ == '__main__':
    main()
