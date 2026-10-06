# SPDX-License-Identifier: MIT
"""The camera and audio contracts from the Linux side (quality/contracts/camera.json, audio.json): the
real shared/media/media-bridge.py against tools/contracts.py's stand-ins of the platform bridge and of
the app's capture socket (its fixed path routed to the stand-in). PCM goes through real pipes, as
between media-bridge and PulseAudio's pipe source and sink. The providers' side is the acceptance
scenarios contract.camera, contract.audio, camera.frames, audio.playback and audio.record on the phone."""
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tools/tests'))
import contracts  # noqa: E402
from test_contract_network import load  # noqa: E402


def media_bridge(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setitem(sys.modules, 'rungic_host_watch', load('rungic_host_watch', ROOT / 'shared/platform/host_watch.py'))
    module = load('media_bridge', ROOT / 'shared/media/media-bridge.py')
    for name in ('FIFO', 'PHONE_FIFO'):
        path = tmp_path / getattr(module, name).name
        os.mkfifo(path, 0o600)
        monkeypatch.setattr(module, name, path)
    return module


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.02)
    return condition()


class Process:
    def __init__(self, args):
        self.args, self.stopped = args, False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


# covers[consumer]: iface:camera
def test_each_android_camera_gets_a_camera_source_while_allowed(monkeypatch, tmp_path):
    module = media_bridge(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(module, 'subprocess', types.SimpleNamespace(
        Popen=lambda args: started.append(Process(args)) or started[-1], TimeoutExpired=subprocess.TimeoutExpired))
    with contracts.StandIn('camera') as android:
        monkeypatch.setenv('RUNGIC_PLATFORM_SOCKET', android.path)
        info = module.host_info()
    cameras = module.Cameras()
    cameras.update(info)
    assert [p.args for p in started] == [['/usr/bin/rungic-camera-source', '0', '1920', '1080', '90', 'back'],
                                         ['/usr/bin/rungic-camera-source', '1', '1280', '720', '270', 'front']]
    # Denied in Android and no permission: the sources stop; nothing new starts.
    cameras.update({**info, 'cameraDenied': True, 'cameraPermission': False})
    assert all(p.stopped for p in started) and cameras.children == {}
    # Behind other apps (not visible): none either, even once the retry delay has passed.
    cameras.retry_at = {}
    cameras.update({**info, 'visible': False})
    assert len(started) == 2
    assert android.requests == [{'op': 'capture-info'}] and android.problems == []


# covers[consumer]: iface:audio
def test_android_microphone_pcm_reaches_the_pulseaudio_pipe(monkeypatch, tmp_path):
    module = media_bridge(monkeypatch, tmp_path)
    pcm = bytes(range(256)) * 75          # 200 ms of 48 kHz mono s16le
    finished = threading.Event()

    def microphone(conn, stream, request):
        conn.sendall(pcm)
        finished.wait(5)                  # the stream stays open until Linux stops recording

    with contracts.StandIn('audio', socket_name='capture', streams={'microphone': microphone}) as capture:
        contracts.route(monkeypatch, capture)
        reader = os.open(module.FIFO, os.O_RDONLY | os.O_NONBLOCK)
        microphone_ = module.Microphone()
        microphone_.set_wanted(True)
        got = bytearray()

        def read():
            try:
                got.extend(os.read(reader, 65536))
            except BlockingIOError:
                pass
            return len(got) >= len(pcm)
        assert wait_for(read)
        finished.set()
        microphone_.set_wanted(False)
        os.close(reader)
    assert bytes(got) == pcm
    assert capture.requests == [{'op': 'microphone'}] and capture.problems == []


# covers[consumer]: iface:audio
def test_a_microphone_in_another_format_is_refused(monkeypatch, tmp_path):
    module = media_bridge(monkeypatch, tmp_path)
    stereo = {'ok': True, 'rate': 48000, 'channels': 2, 'format': 's16le'}
    sent = threading.Event()

    def microphone(conn, stream, request):
        conn.sendall(b'\x01\x02' * 4800)
        sent.set()

    with contracts.StandIn('audio', {'microphone': stereo}, socket_name='capture', streams={'microphone': microphone}) as capture:
        contracts.route(monkeypatch, capture)
        reader = os.open(module.FIFO, os.O_RDONLY | os.O_NONBLOCK)
        microphone_ = module.Microphone()
        microphone_.set_wanted(True)
        assert wait_for(lambda: microphone_.retry_at > 0)
        microphone_.thread.join(3)
        try:
            leaked = os.read(reader, 65536)
        except BlockingIOError:
            leaked = b''
        os.close(reader)
    assert leaked == b'', 'PCM of an unexpected format never reaches PulseAudio'


# covers[consumer]: iface:audio
def test_the_phone_sink_plays_on_the_phone(monkeypatch, tmp_path):
    module = media_bridge(monkeypatch, tmp_path)
    pcm = bytes(range(256)) * 150         # 100 ms of 48 kHz stereo s16le
    received, finished = bytearray(), threading.Event()

    def phone_output(conn, stream, request):
        while len(received) < len(pcm):
            chunk = stream.read1(65536)
            if not chunk:
                break
            received.extend(chunk)
        finished.wait(5)

    with contracts.StandIn('audio', socket_name='capture', streams={'phone-output': phone_output}) as capture:
        contracts.route(monkeypatch, capture)
        phone = module.PhoneOutput()
        phone.set_wanted(True)
        # PulseAudio's pipe sink writes what the android_phone sink plays.
        writer = threading.Thread(target=lambda: Path(module.PHONE_FIFO).write_bytes(pcm), daemon=True)
        writer.start()
        assert wait_for(lambda: len(received) >= len(pcm))
        finished.set()
        phone.set_wanted(False)
    assert bytes(received) == pcm
    assert capture.requests == [{'op': 'phone-output'}] and capture.problems == []


# PulseAudio as media-bridge uses it: the pipe source and sink it loads (their FIFOs made, as PulseAudio
# does), the phone sink suspended, and a recording stream on android_microphone while $FAKE_PA/recording
# exists. `pactl subscribe` reports nothing; media-bridge checks again every RECHECK seconds.
PACTL = '''import os, sys, time
state, args = os.environ['FAKE_PA'], sys.argv[1:]
def has(name):
    return os.path.exists(os.path.join(state, name))
if args[:1] == ['subscribe']:
    time.sleep(3600)
elif args[:1] == ['load-module']:
    path = next(a[5:] for a in args if a.startswith('file='))
    if not os.path.exists(path):
        os.mkfifo(path)
    kind = 'sources' if args[1] == 'module-pipe-source' else 'sinks'
    open(os.path.join(state, kind), 'w').close()
    print(17 if kind == 'sources' else 18)
elif args == ['list', 'short', 'sources'] and has('sources'):
    print('1\\tandroid_microphone\\tmodule-pipe-source.c\\ts16le 1ch 48000Hz\\tRUNNING')
elif args == ['list', 'short', 'sinks'] and has('sinks'):
    print('2\\tandroid_phone\\tmodule-pipe-sink.c\\ts16le 2ch 48000Hz\\tSUSPENDED')
elif args == ['list', 'source-outputs'] and has('recording'):
    print('Source Output #5\\n\\tDriver: protocol-native.c\\n\\tSource: 1\\n\\tCorked: no')
'''


# covers[consumer]: iface:audio
def test_the_android_microphone_is_open_only_while_android_permits(monkeypatch, tmp_path):
    """A Linux program records from android_microphone: media-bridge opens Android's microphone while
    capture-info says the desktop is in front and permitted and no call holds the audio, and closes
    it as soon as one of them changes (the app's capture state, through capture-info)."""
    module = media_bridge(monkeypatch, tmp_path)
    monkeypatch.setattr(module, 'RECHECK', 0.3)
    monkeypatch.setattr(module, 'signal', types.SimpleNamespace(signal=lambda *args: None, SIGTERM=15, SIGINT=2))
    bin_dir, pulse = tmp_path / 'bin', tmp_path / 'pa'
    bin_dir.mkdir()
    pulse.mkdir()
    (bin_dir / 'pactl').write_text(f'#!{sys.executable}\n{PACTL}')
    (bin_dir / 'pactl').chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}:{os.environ["PATH"]}')
    monkeypatch.setenv('FAKE_PA', str(pulse))
    (pulse / 'recording').touch()
    info = dict(next(q for q in contracts.load('audio')['queries'] if q['name'] == 'capture-info')['reply'])
    opened, closed, stopping = [], [], threading.Event()

    def microphone(conn, stream, request):
        opened.append(time.monotonic())
        try:
            while not stopping.is_set():
                conn.sendall(b'\0' * 1920)      # 20 ms of silence
                time.sleep(0.02)
        except OSError:
            pass
        closed.append(time.monotonic())

    def drain():                            # PulseAudio's pipe source reads what media-bridge writes
        fd = os.open(module.FIFO, os.O_RDONLY | os.O_NONBLOCK)
        while not stopping.is_set():
            try:
                os.read(fd, 65536)
            except BlockingIOError:
                time.sleep(0.01)
        os.close(fd)

    def still(count, seconds=1.5):
        time.sleep(seconds)
        return len(opened) == count

    with contracts.StandIn('audio', handler=lambda name, request: dict(info) if name == 'capture-info' else None) as android, \
            contracts.StandIn('audio', socket_name='capture', streams={'microphone': microphone}) as capture:
        monkeypatch.setenv('RUNGIC_PLATFORM_SOCKET', android.path)
        monkeypatch.setattr(sys.modules['rungic_host_watch'], 'SOCKET', android.path)
        contracts.route(monkeypatch, capture)
        threading.Thread(target=drain, daemon=True).start()
        bridge = threading.Thread(target=module.main, daemon=True)
        bridge.start()
        try:
            assert wait_for(lambda: len(opened) == 1, 10), 'recording opens the Android microphone'
            info['communication'] = {'active': True}             # a call takes the audio
            assert wait_for(lambda: len(closed) == 1, 10), 'a communication call closes it'
            info.update(communication={'active': False}, visible=False)   # the desktop behind other apps
            assert still(1), 'not reopened while the desktop is not in front'
            info.update(visible=True, microphoneDenied=True, microphonePermission=False)
            assert still(1), 'not reopened while Android denies the microphone'
            info.update(microphoneDenied=False, microphonePermission=True)
            assert wait_for(lambda: len(opened) == 2, 12), 'reopened once all allow it again'
            (pulse / 'recording').unlink()                     # the Linux program stops recording
            assert wait_for(lambda: len(closed) == 2, 10), 'closed when nobody records'
        finally:
            module.stop()
            stopping.set()
            bridge.join(10)
    assert not bridge.is_alive()
    assert [r for r in capture.requests if r['op'] == 'microphone'] == [{'op': 'microphone'}] * 2
    assert {'op': 'capture-info'} in android.requests
    assert android.problems == [] and capture.problems == []


# covers: desktop.host-bridges/E6
def test_capture_goes_to_the_media_backend_and_to_an_older_app_without_it(monkeypatch, tmp_path):
    module = media_bridge(monkeypatch, tmp_path)
    monkeypatch.delenv('RUNGIC_CAPTURE_SOCKET', raising=False)
    backend, app = tmp_path / 'media/capture.sock', tmp_path / 'app/capture.sock'
    monkeypatch.setattr(module, 'CAPTURE_SOCKETS', (str(backend), str(app)))
    app.parent.mkdir(); app.touch()
    assert module.capture_socket() == str(app)        # an app from before the backend
    backend.parent.mkdir(); backend.touch()
    assert module.capture_socket() == str(backend)    # the backend's, whatever the app
    monkeypatch.setenv('RUNGIC_CAPTURE_SOCKET', '/run/stand-in.sock')
    assert module.capture_socket() == '/run/stand-in.sock'
