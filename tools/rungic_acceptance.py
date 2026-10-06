#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Post-release acceptance on the phone (docs/61): scenarios from release/acceptance.json.

  rungic_acceptance.py smoke [--release V]     every deploy; about two minutes
  rungic_acceptance.py full [--release V]      release candidates: smoke plus the full scenarios
  rungic_acceptance.py run ID... [--release V] selected scenarios
  rungic_acceptance.py compare A B             metrics of two reports (paths)
  rungic_acceptance.py render REPORT           readable report.md next to REPORT (no device access)

A check returns passed/metrics/details; metrics are compared with the newest report of an
earlier release. Results: .work/acceptance/<release>/<time>/report.json. Every scenario
restores what it changes (accessibility, display scale, recordings it made). Items that
automatic checks do not replace are listed as manual in each report.
"""
import argparse
import datetime
import json
import re
import shlex
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rungic_agent  # noqa: E402
import rungic_device  # noqa: E402
from rungic_device import out, run  # noqa: E402

SCENARIOS = rungic_device.WORKSPACE / 'release/acceptance.json'
RESULTS = rungic_device.WORKSPACE / '.work/acceptance'
CHECKS = {}


def check(fn):
    CHECKS[fn.__name__] = fn
    return fn


def result(passed, metrics=None, **details):
    return {'passed': bool(passed), 'metrics': metrics or {}, 'details': details}


def user(script, timeout=120):
    return run(script, 'user', timeout, check=False)


def wait_for(condition, timeout=10, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    return condition()


# ---------------------------------------------------------------- session

@check
def session_ready(ctx, settle_s=15, timeout=90):
    """KWin and plasmashell up with stable PIDs, and plasmashell running long enough to have
    loaded its launcher model: later checks must not race a session that is still starting."""
    probe = ('{ test -f /run/user/1000/rungic-session.env || test -f /run/user/1000/moto-session.env; } && echo env; k=$(pidof kwin_wayland) && echo kwin $k; '
             'p=$(pidof -s plasmashell) && echo shell $p $(ps -o etimes= -p $p)')
    samples, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = run(probe, 'container', check=False).stdout
        state = {line.split()[0]: line.split()[1:] for line in text.splitlines() if line.strip()}
        key = (tuple(state.get('kwin', [])), (state.get('shell') or [None])[0])
        age = int(state['shell'][1]) if len(state.get('shell', [])) > 1 else 0
        samples = (samples + [key])[-3:]
        if {'env', 'kwin', 'shell'} <= set(state) and len(samples) == 3 and len(set(samples)) == 1 \
                and age >= settle_s:
            return result(True, {'shell_age_s': age}, kwin=list(key[0]), plasmashell=key[1])
        time.sleep(2)
    return result(False, present=sorted(state), samples=[list(map(str, s)) for s in samples])


@check
def user_units(ctx, critical=()):
    # A release rolled back to has the names from before the Rungic rename (docs/70).
    critical = [n for p in critical for n in {p, p.replace('rungic-', 'moto-')}]
    failed = user('systemctl --user --failed --no-legend --plain | cut -d" " -f1').stdout.split()
    bad = [u for u in failed if any(re.fullmatch(p.replace('*', '.*'), u) for p in critical)]
    return result(not bad, {'failed_units': len(failed)}, failed=failed, critical_failed=bad)


@check
def new_crashes(ctx):
    since = max(1.0, time.time() - ctx['since'] + 2)   # relative, so host and phone clocks need not agree
    groups = rungic_agent.crash_groups(since)['groups']
    known = ctx['spec'].get('known_crash_signatures', {})
    new = [g for g in groups if g['signature'] not in known]
    return result(not new, {'crash_groups': len(groups), 'unknown': len(new)},
                  unknown=[{k: g[k] for k in ('signature', 'comm', 'signal', 'count', 'frames', 'latest_report')}
                           for g in new],
                  known=[{'signature': g['signature'], 'count': g['count'], 'why': known[g['signature']]}
                         for g in groups if g['signature'] in known])


# ---------------------------------------------------------------- interface contracts

# Sends the read-only socket queries of a contract from the container, as the desktop user (as the
# Linux consumers do), and prints one JSON line per query. Private reply fields are blanked there, on
# the phone, keeping their types: the clipboard's text or a caller's number never reaches this computer.
CONTRACT_ASK = r"""python3 - <<'ASK'
import json, os, socket, struct
def blank(value):
    if isinstance(value, str):
        return ''
    if isinstance(value, list):
        return [blank(v) for v in value]
    if isinstance(value, dict):
        return {k: blank(v) for k, v in value.items()}
    return value
for ask in json.loads(%s):
    out = {'name': ask['name']}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(ask['timeout'])
            c.connect(os.environ.get(ask['env'], ask['address']) if ask['env'] else ask['address'])
            if ask['peer_uid'] is not None:
                out['peer_uid'] = struct.unpack('3i', c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            c.sendall(json.dumps(ask['request']).encode() + b'\n' + bytes(ask['payload']))
            reply = json.loads(c.makefile('rb').readline() or b'null')
            for key in ask['private'] if isinstance(reply, dict) else ():
                if key in reply:
                    reply[key] = blank(reply[key])
            out['reply'] = reply
    except (OSError, ValueError) as error:
        out['error'] = repr(error)
    print(json.dumps(out))
ASK"""


def _parse_command(q, text):
    """A command query's reply: its last JSON line, or its key=value lines (format properties)."""
    if q.get('format', 'json') == 'properties':
        reply = dict(line.split('=', 1) for line in text.splitlines() if '=' in line and not line.startswith('#'))
    else:
        lines = [line for line in text.splitlines() if line.startswith('{')]
        reply = json.loads(lines[-1]) if lines else None
    for key in q.get('private', []) if isinstance(reply, dict) else ():
        if isinstance(reply.get(key), str):
            reply[key] = ''        # not kept in the report (a command's output does reach this computer)
    return reply


def _command_problems(q):
    done = run(q['command'], q.get('as', 'root'), q.get('timeout', 30), check=False)
    fmt = q.get('format', 'json')
    if fmt == 'exit':
        if done.returncode != q.get('exit', 0):
            return [f'exit {done.returncode}, not {q.get("exit", 0)}: {(done.stdout + done.stderr)[-300:]}']
        return []
    if fmt == 'text':
        return [] if re.search(q['expect'], done.stdout) else [f'output does not match {q["expect"]!r}: {done.stdout[-300:]!r}']
    if done.returncode != 0:
        return [f'exit {done.returncode}: {(done.stdout + done.stderr)[-300:]}']
    import contracts
    try:
        return contracts.check_reply(q, _parse_command(q, done.stdout))
    except ValueError as error:
        return [f'unreadable output: {error}']


@check
def interface_contract(ctx, interface='platform-bridge'):
    """The Android provider of an interface keeps its contract (quality/contracts/, tools/contracts.py):
    each read-only query of the contract, sent from the container as the Linux consumers send it (or
    its read-only command, run where the contract says), gets a reply with every field they rely on, of
    the right type, from the server they trust (peer_uid). Queries not marked read_only are never sent:
    nothing changes on the phone. The consumers' side is tested offline against a stand-in of the same
    contract."""
    import contracts
    contract = contracts.load(interface)
    read = [q for q in contract['queries'] if q.get('read_only') is True]
    asks = []
    for q in read:
        if 'command' not in q:
            spec = contracts.socket_of(contract, q)[1]
            asks.append({'name': q['name'], 'request': q['request'], 'address': contracts.address(spec),
                         'env': spec.get('env'), 'peer_uid': spec.get('peer_uid'), 'timeout': q.get('timeout', 5),
                         'payload': int(q['request'].get(q['payload'], 0)) if q.get('payload') else 0,
                         'private': q.get('private', [])})
    answers = {}
    if asks:
        wait = sum(a['timeout'] for a in asks) + 30
        for line in user(CONTRACT_ASK % repr(json.dumps(asks)), timeout=wait).stdout.splitlines():
            if line.startswith('{'):
                answer = json.loads(line)
                answers[answer['name']] = answer
    problems = {}
    for q in read:
        name = q['name']
        if 'command' in q:
            found = _command_problems(q)
        else:
            answer, spec = answers.get(name), contracts.socket_of(contract, q)[1]
            if answer is None or 'error' in answer:
                found = [answer.get('error') if answer else 'no answer']
            elif spec.get('peer_uid') is not None and answer.get('peer_uid') != spec['peer_uid']:
                found = [f"served by uid {answer.get('peer_uid')}, not {spec['peer_uid']}"]
            elif isinstance(answer['reply'], dict) and 'error' in answer['reply'] \
                    and not any('error' in r for r in contracts.replies(q)):
                found = [f"error reply: {answer['reply']['error']}"]
            else:
                found = contracts.check_reply(q, answer['reply'])
        if found:
            problems[name] = found
    details = {} if read else {'error': 'the contract has no read-only query'}
    return result(read and not problems, {'queries': len(read), 'broken': len(problems)},
                  interface=interface, problems=problems, **details)


# ---------------------------------------------------------------- display and input

@check
def display_geometry(ctx):
    android = re.search(r'(\d+)x(\d+)', out('wm size | tail -1', 'shell'))
    android = (int(android[1]), int(android[2])) if android else None
    doctor = json.loads(user('kscreen-doctor -j 2>/dev/null').stdout or '{}')
    outputs = []
    for o in doctor.get('outputs', []):
        if not o.get('enabled'):
            continue
        mode = next((m for m in o.get('modes', []) if m['id'] == o.get('currentModeId')), None)
        if mode:
            outputs.append({'name': o['name'], 'size': [mode['size']['width'], mode['size']['height']],
                            'scale': o.get('scale'), 'rotation': o.get('rotation'), 'refresh': mode.get('refreshRate')})
    phone = [o for o in outputs if sorted(o['size']) == sorted(android or ())]
    return result(bool(phone), {'refresh_hz': phone[0]['refresh'] if phone else None},
                  android=android, outputs=outputs)


def _home():
    """A known starting point: keyboard hidden, drawer closed, home screen."""
    import ui_launch_check as ui
    run(f'{rungic_device.PLASMA} hide-keyboard', 'root', check=False)
    for _ in range(2):
        # Folio toggles the drawer if Home is pressed while already at page zero.
        # The suggestions handle positively identifies the new home screen.
        if rungic_agent.ui_find('plasmashell', role='button', name='^打开应用抽屉$'):
            break
        # Native Folio home has no custom handle. Home toggles its drawer, so do
        # not press it when the desktop is already focused and search is closed.
        if any(w['active'] and w['resource_class'] == 'plasmashell' for w in rungic_agent.ui_windows()) \
                and not rungic_agent.ui_find('plasmashell', role='text', name='Search'):
            break
        ui.press('Home')
        time.sleep(0.8)


def _drawer_search():
    import ui_launch_check as ui
    _home()
    try:
        ui.open_drawer()
    except RuntimeError:
        _home()
        ui.open_drawer()
    fields = rungic_agent.ui_find('plasmashell', role='text', name='Search')
    if not fields:
        raise RuntimeError('drawer search field not showing')
    return fields[0]


OCR = """
import json, sys
from rapidocr import RapidOCR
out = RapidOCR()(sys.argv[1])
print(json.dumps([[t, float(sc), [int(v) for v in b[0]]] for t, sc, b in zip(out.txts, out.scores, out.boxes)],
                 ensure_ascii=False))
"""


def ocr_screen():
    """Text on the phone's screen: [text, score, [x, y]] from RapidOCR in rungic-clicker's venv."""
    shot = rungic_agent.screenshot()
    rungic_device.to_container(shot, '/var/tmp/rungic-acceptance-ocr.png', '644')
    text = user('py=/usr/lib/rungic-clicker/venv/bin/python; [ -x $py ] || py=/usr/lib/moto-clicker/venv/bin/python; '
                f"$py -c {shlex.quote(OCR)} "
                '/var/tmp/rungic-acceptance-ocr.png; code=$?; rm -f /var/tmp/rungic-acceptance-ocr.png; exit $code', timeout=120)
    lines = text.stdout.strip().splitlines()
    if text.returncode or not lines:
        raise RuntimeError('OCR failed: ' + (text.stderr or text.stdout)[-1800:])
    return json.loads(lines[-1]), shot


@check
def input_text(ctx, text='Calcul', expect='Calculator', absent='Clock'):
    """Android text input into the drawer search, read back from the screen by OCR: right after a
    session restart the results never reach the AT-SPI tree, and the search field exposes no text."""
    enabled = rungic_agent.a11y('state')['enabled']
    if not enabled:
        rungic_agent.ui_enable(True)
        time.sleep(2)
    try:
        field = _drawer_search()
        taps = 0
        for taps in range(1, 4):
            rungic_agent.ui_tap('plasmashell', field['path'])
            focused = wait_for(lambda: any('focused' in f.get('states', []) for f in
                                           rungic_agent.ui_find('plasmashell', role='text', name='Search')),
                               timeout=3, interval=0.3)
            if focused:
                break
        if not focused:
            return result(False, {'taps': taps}, error='the drawer search field never took focus')
        run(f'input text {shlex.quote(text)}', 'shell')
        time.sleep(1.5)
        words, _ = ocr_screen()
        top = field['extents'][1] * 3 + 400          # logical → pixels, generous: field and results
        seen = [w for w, score, (x, y) in words if y < top + 600]
        # Case-insensitive: right after a container start the first Android key input sometimes
        # arrives with the wrong case ("CaICUL"); that is recorded, text delivery is what is checked.
        typed = [w for w in seen if w.lower().startswith(text.lower()) and not w.lower().startswith(expect.lower())]
        return result(bool(typed) and expect in seen and absent not in seen, {'taps': taps}, sent=text,
                      case_exact=any(w.startswith(text) for w in typed), seen=seen[:20])
    finally:
        try:
            _home()
        except Exception:
            pass
        if not enabled:
            rungic_agent.ui_enable(False)


# ---------------------------------------------------------------- media

CAMERA_PROBE = r'''
import gi, json, sys, time
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
node, count = sys.argv[1], int(sys.argv[2])
pipe = Gst.parse_launch(f'pipewiresrc target-object={node} num-buffers={count + 5} ! videoconvert ! '
                        'video/x-raw,format=GRAY8 ! appsink name=sink sync=false max-buffers=4 drop=false')
sink = pipe.get_by_name('sink')
pipe.set_state(Gst.State.PLAYING)
frames, start, caps = [], time.monotonic(), None
while len(frames) < count and time.monotonic() - start < 40:
    sample = sink.emit('try-pull-sample', 10 * Gst.SECOND)
    if sample is None:
        break
    caps = caps or sample.get_caps().to_string()
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    data = info.data[::211]
    mean = sum(data) / len(data)
    std = (sum((x - mean) ** 2 for x in data) / len(data)) ** 0.5
    frames.append({'t': round(time.monotonic() - start, 3), 'pts': buf.pts, 'mean': round(mean, 1),
                   'std': round(std, 1)})
    buf.unmap(info)
first = time.monotonic() - start
pipe.set_state(Gst.State.NULL)
print(json.dumps({'frames': frames, 'caps': caps, 'seconds': round(first, 2)}))
'''


def _node_state(name):
    dump = json.loads(user('pw-dump 2>/dev/null').stdout or '[]')
    for obj in dump:
        info = obj.get('info') or {}
        if obj.get('type', '').endswith(':Node') and (info.get('props') or {}).get('node.name') == name:
            return info.get('state')
    return None


@check
def camera_frames(ctx, node='rungic.camera.0', frames=20):
    if _node_state(node) is None and _node_state(node.replace('rungic.', 'moto.', 1)) is not None:
        node = node.replace('rungic.', 'moto.', 1)      # a release from before the rename (docs/70)
    probe = user(f"python3 -c {shlex.quote(CAMERA_PROBE)} {shlex.quote(node)} {int(frames)}", timeout=90)
    try:
        data = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return result(False, error=(probe.stderr or probe.stdout)[-1500:])
    got = data['frames']
    pts = [f['pts'] for f in got]
    monotonic = all(b > a for a, b in zip(pts, pts[1:]))
    # Not a constant fill: sensor noise or scene changes. A dark scene (phone face down) still
    # has noise; a pipeline that delivers zeroed or stale buffers has none.
    varied = sum(1 for f in got if f['std'] > 0) + len({f['mean'] for f in got}) - 1
    fps = (len(pts) - 1) / ((pts[-1] - pts[0]) / 1e9) if len(pts) > 1 and pts[-1] > pts[0] else None
    idle = wait_for(lambda: _node_state(node) in ('suspended', 'idle'), timeout=10)
    luma = round(sum(f['mean'] for f in got) / len(got), 1) if got else None
    arrival = [f['t'] for f in got]
    max_gap = round(max((b - a for a, b in zip(arrival, arrival[1:])), default=0), 3)
    return result(len(got) == frames and monotonic and varied >= frames // 2 and idle,
                  {'fps': round(fps, 1) if fps else None, 'first_frame_s': arrival[0] if arrival else None,
                   'max_gap_s': max_gap, 'mean_luma': luma},
                  frames=len(got), monotonic=monotonic, varied=varied, caps=data['caps'],
                  state_after=_node_state(node))


def _pactl_short(kind):
    rows = []
    for line in user(f'pactl list short {kind}').stdout.splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            rows.append(parts)
    return rows


@check
def audio_playback(ctx):
    default = user('pactl get-default-sink').stdout.strip()
    sinks = {row[1]: row for row in _pactl_short('sinks')}
    # 1 s of a quiet 440 Hz tone at 5 % stream volume: enough to route, barely audible.
    user('''python3 - <<'PY'
import math, struct, wave
with wave.open('/tmp/rungic-acceptance-tone.wav', 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
    w.writeframes(b''.join(struct.pack('<hh', v, v) for v in
                  (int(800 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(48000 * 2))))
PY''')
    import threading
    player = threading.Thread(target=user, args=('paplay --volume=3277 --client-name=rungic-acceptance '
                                                 '/tmp/rungic-acceptance-tone.wav',), daemon=True)
    player.start()
    stream = wait_for(lambda: [r for r in _pactl_short('sink-inputs')], timeout=4, interval=0.2)
    sink_index = stream[0][1] if stream else None
    sink_name = next((name for name, row in sinks.items() if row[0] == sink_index), None)
    player.join(15)
    user('rm -f /tmp/rungic-acceptance-tone.wav')
    suspended = wait_for(lambda: dict((r[1], r[-1]) for r in _pactl_short('sinks')).get(default) in
                         ('SUSPENDED', 'IDLE'), timeout=12)
    return result(bool(stream) and sink_name == default and suspended, default_sink=default,
                  stream_sink=sink_name, state_after=dict((r[1], r[-1]) for r in _pactl_short('sinks')).get(default))


@check
def audio_record(ctx):
    default = user('pactl get-default-source').stdout.strip()
    rec = user('timeout 2.5 parecord --raw --format=s16le --channels=1 --rate=48000 --client-name=rungic-acceptance '
               '/tmp/rungic-acceptance.raw; python3 -c "import struct,sys; d=open(\'/tmp/rungic-acceptance.raw\','
               '\'rb\').read(); n=len(d)//2; s=struct.unpack(\'<%dh\'%n, d[:n*2]); '
               'print(n, max(map(abs, s)) if s else 0, (sum(x*x for x in s)/max(n,1))**0.5)"; '
               'rm -f /tmp/rungic-acceptance.raw', timeout=30).stdout.split()
    samples, peak, rms = (int(rec[0]), int(rec[1]), float(rec[2])) if len(rec) == 3 else (0, 0, 0.0)
    suspended = wait_for(lambda: dict((r[1], r[-1]) for r in _pactl_short('sources')).get(default) in
                         ('SUSPENDED', 'IDLE'), timeout=12)
    return result(samples > 48000 and peak > 0 and suspended, {'rms': round(rms, 1), 'peak': peak},
                  default_source=default, samples=samples)


def _keep_screen_on():
    apk = rungic_device.apk()
    text = run(f"dumpsys window windows | grep -A30 '{apk}/{apk}.MainActivity' "
               "| grep -m1 -o 'fl=[^ ]*'", 'shell', 30, check=False).stdout
    return 'KEEP_SCREEN_ON' in text


@check
def idle_inhibit(ctx, seconds=6):
    """A Wayland client inhibiting idle keeps the phone screen on, and only while it does (docs/72):
    through KWin to the Android host, whichever way KWin forwards it."""
    before = _keep_screen_on()
    import threading
    probe = {}
    thread = threading.Thread(target=lambda: probe.setdefault(
        'result', user(f"WAYLAND_DISPLAY=wayland-0 {rungic_device.prog('idle-probe')} {seconds}", timeout=seconds + 30)), daemon=True)
    thread.start()
    during = wait_for(_keep_screen_on, timeout=seconds - 1, interval=0.5)
    thread.join(seconds + 30)
    after = wait_for(lambda: not _keep_screen_on(), timeout=5, interval=0.5)
    ran = probe.get('result')
    ok = ran is not None and ran.returncode == 0
    return result(ok and not before and during and after, before=before, during=during, released=after,
                  probe=(ran.stdout + ran.stderr).strip()[-300:] if ran else 'no result',
                  note='inconclusive: the screen was kept on before the probe' if before else '')


# ---------------------------------------------------------------- full level

def _cast_outputs():
    try:
        outputs = json.loads(user('kscreen-doctor -j', timeout=20).stdout).get('outputs', [])
    except ValueError:
        return []
    return [o for o in outputs if o.get('name', '').startswith('CAST') and o.get('enabled')]


@check
def desktop_mode(ctx, timeout=90):
    """Desktop mode is the independent desktop, workspace 0 (docs/research/97 §19): turning it on
    starts rungic-workspace@0 with its own plasmashell and the floating window that shows it, and
    adds no output to the phone's KWin; turning it off ends all of it. In use already: checks it is
    all there and leaves it as it was."""
    program = '"$(command -v rungic-desktop-mode)"'

    def state(command):
        out = user(f'{program} {command}', timeout=timeout).stdout
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {'unreadable': out[-300:]}

    def parts():
        text = user('systemctl --user is-active -q rungic-workspace@0 && echo unit; '
                    'pgrep -f "^plasmashell -p org.kde.plasma.desktop" >/dev/null && echo shell; '
                    'test -S "$XDG_RUNTIME_DIR/wayland-ws-0" && echo socket; '
                    'pgrep -f "^/usr/libexec/rungic-agent-screen-window --desktop" >/dev/null && echo window').stdout
        return {name: name in text.split() for name in ('unit', 'shell', 'socket', 'window')}

    # What runs decides, not only what status says: a desktop the user has open is never turned off.
    # Unreadable state: nothing is switched (a status read as {} once turned the user's desktop off).
    now, before = parts(), state('status')
    if 'unreadable' in before:
        return result(False, error='desktop mode status unreadable; nothing switched', status=before, parts=now)
    if before.get('enabled') or now['unit']:
        now = parts()
        return result(all(now.values()) and not _cast_outputs(), note='already on; left as it was', parts=now,
                      cast_outputs=[o['name'] for o in _cast_outputs()])
    on = state('on')
    up = wait_for(lambda: (lambda p: p if all(p.values()) else None)(parts()), timeout=60, interval=2) or parts()
    cast = [o['name'] for o in _cast_outputs()]
    off = state('off')
    gone = wait_for(lambda: not any(parts().values()), timeout=40, interval=2)
    return result(all(up.values()) and not cast and gone, up=up, cast_outputs=cast, gone=bool(gone), on=on, off=off)


@check
def app_launch(ctx, app='Calculator', process='kalk', rounds=2):
    """Launch and close through the launcher by accessible names (tools/ui_launch_check.py)."""
    import ui_launch_check as ui
    enabled = rungic_agent.a11y('state')['enabled']
    if not enabled:
        rungic_agent.ui_enable(True)
        time.sleep(2)
    try:
        if ui.running(process):
            ui.close(process)
        steps = []
        for _ in range(rounds):
            began = time.monotonic()
            step = ui.launch(app, process, False)
            step['launch_s'] = round(time.monotonic() - began, 1)
            step |= ui.close(process)
            steps.append(step)
        ok = all(s['started'] and s['registered'] and s['exited'] for s in steps)
        return result(ok, {'launch_s': max(s['launch_s'] for s in steps)}, rounds=steps)
    finally:
        try:
            _home()
        except Exception:
            pass
        if not enabled:
            rungic_agent.ui_enable(False)


def _phone_output():
    doctor = json.loads(user('kscreen-doctor -j 2>/dev/null').stdout or '{}')
    android = re.search(r'(\d+)x(\d+)', out('wm size | tail -1', 'shell'))
    size = sorted((int(android[1]), int(android[2]))) if android else None
    for o in doctor.get('outputs', []):
        mode = next((m for m in o.get('modes', []) if m['id'] == o.get('currentModeId')), None)
        if o.get('enabled') and mode and sorted((mode['size']['width'], mode['size']['height'])) == size:
            return o
    return None


@check
def display_scale_roundtrip(ctx, other=2.75):
    """KScreen applies a scale to the phone output and reverts it: the display settings path
    (KScreen -> KWin output management -> the Android host) works both ways."""
    output = _phone_output()
    if not output:
        return result(False, error='phone output not found in kscreen-doctor')
    name, original = output['name'], output['scale']
    target = other if abs(original - other) > 0.01 else original - 0.25
    user(f'kscreen-doctor output.{name}.scale.{target}')
    applied = wait_for(lambda: (lambda o: o and abs(o['scale'] - target) < 0.01)(_phone_output()), timeout=8)
    user(f'kscreen-doctor output.{name}.scale.{original}')
    restored = wait_for(lambda: (lambda o: o and abs(o['scale'] - original) < 0.01)(_phone_output()), timeout=8)
    return result(bool(applied) and bool(restored), output=name, original=original, tried=target,
                  applied=bool(applied), restored=bool(restored))


def _host_display():
    try:
        return json.loads(user('cat /mnt/android-wayland/android-display.json').stdout)
    except ValueError:
        return {}


@check
def display_refresh_policy(ctx, fixed=60):
    """The refresh policy through the stock display settings path (docs/73): KScreen's adaptive
    refresh (VRR) policy Never with a mode of a fixed rate makes Android's refresh policy that rate,
    Automatic makes it Android's automatic rate (0); the original policy is restored."""
    output = _phone_output()
    if not output:
        return result(False, error='phone output not found in kscreen-doctor')
    name = output['name']
    before = _host_display()
    original = before.get('refreshPolicy')
    mode = next(m for m in output['modes'] if m['id'] == output['currentModeId'])
    size = f"{mode['size']['width']}x{mode['size']['height']}"
    vrr = 'capabilities' in output and output.get('vrrPolicy') is not None

    def policy_is(value):
        return wait_for(lambda: _host_display().get('refreshPolicy') == value, timeout=10)
    user(f'kscreen-doctor output.{name}.vrrpolicy.never output.{name}.mode.{size}@{fixed}')
    set_fixed = policy_is(fixed)
    user(f'kscreen-doctor output.{name}.vrrpolicy.automatic')
    set_auto = policy_is(0)
    if original:
        user(f'kscreen-doctor output.{name}.vrrpolicy.never output.{name}.mode.{size}@{original}')
    restored = policy_is(original)
    return result(bool(set_fixed) and bool(set_auto) and bool(restored),
                  output=name, original=original, fixed=bool(set_fixed), automatic=bool(set_auto),
                  restored=bool(restored), vrr_in_kscreen=vrr)


# Names that stay "moto" in phase B of the Rungic rename (docs/70): the Android side creates or reads
# them (bind mounts, the Magisk launcher's directory), or they belong to no package (manual leftovers).
RESIDUE_ALLOWED = re.compile(r'^/(var/lib/moto-(host|cores|apt)|opt/moto-)')
RESIDUE = r"""
find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /home -o -path /tmp \
  -o -path /var/tmp -o -path /root -o -path /var/lib/moto-apt -o -path /var/lib/moto-cores -o -path /var/cache \
  -o -path /var/lib/dpkg -o -path /var/lib/apt \) -prune -o -iname '*moto*' -print 2>/dev/null | while read -r p; do
  owner=$(dpkg -S "$p" 2>/dev/null | head -1 | cut -d: -f1)
  status=; [ -z "$owner" ] || status=$(dpkg-query -W -f '${db:Status-Abbrev}' "$owner" 2>/dev/null)
  echo "$p	$owner	$status"
done
echo "@@units"; systemctl list-units --all --no-legend --plain 'moto*' | cut -d' ' -f1
echo "@@userunits"; runuser -u "$(id -nu 1000)" -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user list-units --all --no-legend --plain 'moto*' | cut -d' ' -f1
echo "@@packages"; dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' 'moto-*' 2>/dev/null | grep '^ii' | cut -d' ' -f2-
"""


@check
def rebrand_residue(ctx):
    """The Rungic rename (docs/70): no installed package ships a file or unit under a "moto" name, no
    such unit is loaded, no moto-* package is installed, and the Android side has the Rungic layout
    (phase C); the names kept until later phases and unowned leftovers are listed, not failed."""
    text = run(RESIDUE, 'container', timeout=300, check=False).stdout
    files, _, rest = text.partition('@@units')
    units, _, rest = rest.partition('@@userunits')
    user_units, _, packages = rest.partition('@@packages')
    owned, allowed, unowned, removed = [], [], [], []
    for line in files.strip().splitlines():
        path, owner, status = (line.split('\t') + ['', ''])[:3]
        if re.search(r'(?i)motor', path.rsplit('/', 1)[-1]):
            continue                                # motorway, Motorola: words, not our names
        if RESIDUE_ALLOWED.match(path):
            allowed.append(path)
        elif owner and not status.startswith('ii'):
            removed.append(f'{path} ({owner})')     # conffiles of a removed moto-* package, until phase D
        elif owner:
            owned.append(f'{path} ({owner})')
        else:
            unowned.append(path)
    units, user_units, packages = units.split(), user_units.split(), packages.split()
    android = _android_residue()
    return result(not owned and not units and not user_units and not packages and not android['failed'],
                  {'owned': len(owned), 'units': len(units) + len(user_units), 'packages': len(packages),
                   'unowned': len(unowned), 'removed_conffiles': len(removed),
                   'android': len(android['failed']), 'android_later': len(android['later'])},
                  owned=owned[:40], units=units + user_units, packages=packages, allowed=sorted(set(allowed))[:20],
                  removed_conffiles=removed[:40], unowned=unowned[:60], android=android)


# The Android side after the phase C cutover (docs/70, tools/rungic_cutover.py). Later phases: phase D
# (the files the cutover keeps for `down`, the old APK, the Termux audio directory it leaves, the
# container's mounts under the old names, Docker's old volumes and image tag), the next ROM (Magisk
# bootstrap logs), the next boot (debug.moto.* set by hand; nothing reads them); moto-phosh is an old
# leftover.
ANDROID_LATER = re.compile(r'^(/data/adb/(moto-phosh|moto-magisk-|rungic-cutover)'
                           r'|/data/data/com\.termux/files/usr/tmp/moto-(plasma|phosh)-audio|package:dev\.moto\.plasma$'
                           r'|process:lxc-start -n plasma |property:debug\.moto\.)')
ANDROID_RESIDUE = r"""
[ -d /data/adb/rungic-plasma ] || { echo layout:moto; exit 0; }
find /data/adb -maxdepth 2 -iname '*moto*' -print
find /data/adb/rungic-lxc/runtime/var/lib/lxc/plasma -maxdepth 2 -iname '*moto*' -print
ls -d /data/data/com.termux/files/usr/tmp/*moto* 2>/dev/null
pm list packages | grep -x package:dev.moto.plasma
pm list packages -e | grep -x package:dev.moto.plasma | sed 's/^/enabled:/'
ps -A -o ARGS | grep -i -E '[m]oto-|dev[.]moto' | sed 's/^/process:/'
getprop | grep -o '^\[debug[.]moto[.][^]]*' | sed 's/^\[/property:/'
echo "label:$(ls -Z /data/adb/rungic-lxc/images/rootfs.img | cut -d' ' -f1)"
"""


def _android_residue():
    lines = run(ANDROID_RESIDUE, 'root', timeout=120, check=False).stdout.split('\n')
    lines = [line.strip() for line in lines if line.strip()]
    if 'layout:moto' in lines:
        return {'failed': ['the Android side is from before the phase C cutover'], 'later': []}
    failed, later = [], []
    for line in lines:
        if line.startswith('label:'):
            if line != 'label:u:object_r:rungic_image:s0':
                failed.append(line)
        elif ANDROID_LATER.match(line) and not line.startswith('enabled:'):
            later.append(line)
        else:
            failed.append(line)
    return {'failed': failed, 'later': later}

CODEC = r"""
set -e
d=$(mktemp -d /var/tmp/rungic-codec.XXXXXX); trap 'rm -rf "$d"' EXIT
# The names from before the Rungic rename (docs/70) on a release rolled back to.
name=rungic; [ -x /usr/lib/rungic-codec/ffmpeg/bin/ffprobe ] || name=moto
bin=/usr/lib/$name-codec/ffmpeg/bin
export LD_LIBRARY_PATH=$bin/../lib:$bin/../..
now() { python3 -c 'import time; print(time.monotonic())'; }
t0=$(now)
gst-launch-1.0 -q videotestsrc num-buffers=FRAMES pattern=ball ! video/x-raw,width=1280,height=720,framerate=30/1 \
  ! ${name}h264enc ! h264parse ! mp4mux ! filesink location=$d/t.mp4
t1=$(now)
$bin/ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=codec_name,nb_read_frames,width,height \
  -show_entries format=duration -of json $d/t.mp4
t2=$(now)
decoded=$($bin/ffmpeg -v error -c:v h264_$name -i $d/t.mp4 -f framemd5 - 2>/dev/null | grep -vc '^#')
t3=$(now)
echo "@@ encode_s=$(python3 -c "print(round($t1 - $t0, 2))") decode_s=$(python3 -c "print(round($t3 - $t2, 2))") decoded=$decoded"
"""


@check
def codec_roundtrip(ctx, frames=90):
    """Android hardware H.264 encode through the GStreamer element (rungich264enc), then decode through the
    private FFmpeg's h264_rungic: frame counts, duration and resolution checked."""
    text = user(CODEC.replace('FRAMES', str(int(frames))), timeout=180)
    body, _, tail = text.stdout.partition('@@ ')
    try:
        probe = json.loads(body)
    except ValueError:
        return result(False, error=(text.stderr or text.stdout)[-1500:])
    stream = (probe.get('streams') or [{}])[0]
    values = dict(kv.split('=', 1) for kv in tail.split())
    encoded = int(stream.get('nb_read_frames') or 0)
    decoded = int(values.get('decoded') or 0)
    duration = float(probe.get('format', {}).get('duration') or 0)
    ok = (encoded == frames and decoded == frames and stream.get('codec_name') == 'h264'
          and (stream.get('width'), stream.get('height')) == (1280, 720) and abs(duration - frames / 30) < 0.2)
    return result(ok, {'encode_s': float(values.get('encode_s') or 0), 'decode_s': float(values.get('decode_s') or 0)},
                  encoded_frames=encoded, decoded_frames=decoded, duration=duration, stream=stream)


@check
def rime_input(ctx):
    """Chinese input, in two automatic parts: the Rime engine and data commit Chinese first candidates
    (rungic-rime-check: nihao, zhongguo, ceshi, 300 compositions), and focusing a Qt text field
    (rungic-input-probe) brings up the keyboard (its keys appear on AT-SPI). Android key events reach the
    client directly, not through Rime, so typing on the virtual keyboard itself stays a manual item."""
    check = run('for p in /usr/libexec/rungic-rime-check /usr/libexec/moto-rime-check; do [ -x $p ] && '
                'exec $p; done; exit 9', 'user', timeout=120, check=False)
    engine = check.returncode == 0
    enabled = rungic_agent.a11y('state')['enabled']
    if not enabled:
        rungic_agent.ui_enable(True)
        time.sleep(2)
    probe = next((p for p in ('/usr/bin/rungic-input-probe', '/usr/bin/moto-input-probe')
                  if run(f'test -x {p}', 'container', check=False).returncode == 0), None)
    keyboard = False
    # By its command line: the process name is cut to 15 characters ("rungic-input-pr"), so
    # `pkill -x` by the program name never matches (it left a probe window behind each run).
    stop_probe = f'pkill -f -x {probe}' if probe else 'true'
    app = probe.rsplit('/', 1)[1] if probe else None     # its AT-SPI application name
    try:
        if probe:
            run(stop_probe, 'container', check=False)
            user(f'(setsid {probe} >/dev/null 2>&1 &) ; true')
            field = wait_for(lambda: next((f for f in rungic_agent.ui_find(app, role='text')), None)
                             if any(a['name'] == app for a in rungic_agent.a11y('apps')) else None,
                             timeout=20)
            if field:
                rungic_agent.ui_tap(app, field['path'])
                keyboard = bool(wait_for(lambda: [n for n in rungic_agent.ui_find('plasma-keyboard', role='label')
                                                  if n['name'] in ('q', 'a', 'z')], timeout=8))
        return result(engine and keyboard, engine=check.stdout.strip() or f'exit {check.returncode}',
                      keyboard_shown=keyboard, probe=probe)
    finally:
        run(stop_probe, 'container', check=False)
        try:
            _home()
        except Exception:
            pass
        if not enabled:
            rungic_agent.ui_enable(False)


def _quick_settings():
    """The quick settings fully expanded: the first pull shows one row only, and AT-SPI reports the
    tiles of the collapsed part as showing although they are off screen."""
    run('input swipe 300 2 300 1200 400', 'shell')
    time.sleep(1.2)
    run('input swipe 540 500 540 1800 400', 'shell')
    time.sleep(1.5)


def _tap_label(pattern):
    labels = [n for n in rungic_agent.ui_find('plasmashell', role='label', name=pattern)
              if n.get('extents', [0, 0, 0, 0])[2] > 0]
    if not labels:
        raise RuntimeError(f'no visible label {pattern!r}')
    return rungic_agent.ui_tap('plasmashell', labels[0]['path'])


PROBE_RECORDING = r"""
f=$(ls -t "$HOME"/Videos/screen-recording*.mp4 2>/dev/null | grep -v '\.partial\.mp4$' | head -1)
[ -n "$f" ] && [ -s "$f" ] || exit 3
bin=/usr/lib/rungic-codec/ffmpeg/bin; [ -x $bin/ffprobe ] || bin=/usr/lib/moto-codec/ffmpeg/bin
export LD_LIBRARY_PATH=$bin/../lib:$bin/../..
echo "$f"; stat -c %Y "$f"
$bin/ffprobe -v error -show_entries stream=codec_type,codec_name,avg_frame_rate -show_entries format=duration -of json "$f"
"""


@check
def screen_recording(ctx, seconds=4):
    """The recording quick setting, pressed as a user would (AT-SPI finds it, a touch toggles it): a playable
    MP4 with a video and an audio track and about the recorded duration. The file is deleted afterwards."""
    enabled = rungic_agent.a11y('state')['enabled']
    if not enabled:
        rungic_agent.ui_enable(True)
        time.sleep(2)
    started = time.time()
    path = None
    try:
        _home()
        _quick_settings()
        _tap_label('^(录屏|Record Screen)$')   # the tile in the desktop's language
        began = time.monotonic()
        time.sleep(seconds + 1)
        _quick_settings()
        _tap_label('^(正在录屏|Recording)')   # the tile while recording, in either language
        elapsed = time.monotonic() - began   # opening the quick settings takes a while over AT-SPI
        text = wait_for(lambda: (lambda r: r if r.returncode == 0 and float(r.stdout.split('\n')[1]) >= started - 2
                                 else None)(user(PROBE_RECORDING)), timeout=30, interval=2)
        if not text:
            journal = run('journalctl --since=-3min -o cat | grep "^Screen recording:" | tail -8', 'container',
                          check=False).stdout
            partial = user('f=$(ls -t "$HOME"/Videos/screen-recording*.partial.mp4 2>/dev/null | head -1); '
                           '[ -n "$f" ] && echo "$(stat -c %Y "$f") $f"').stdout.strip()
            if partial and float(partial.split(' ', 1)[0]) >= started - 2:
                path = partial.split(' ', 1)[1]   # this run's unfinished file only; older ones may be recoverable
            return result(False, error='no finished screen recording in ~/Videos', recorder_log=journal[-1200:])
        lines = text.stdout.split('\n', 2)
        path = lines[0]
        probe = json.loads(lines[2])
        kinds = {st['codec_type']: st for st in probe.get('streams', [])}
        duration = float(probe.get('format', {}).get('duration') or 0)
        ok = 'video' in kinds and 'audio' in kinds and abs(duration - elapsed) <= 3
        return result(ok, {'duration_s': round(duration, 2)}, between_taps_s=round(elapsed, 1),
                      streams=probe.get('streams'), file=path)
    finally:
        if path:
            user(f'rm -f {shlex.quote(path)}')
        try:
            _home()
        except Exception:
            pass
        if not enabled:
            rungic_agent.ui_enable(False)


@check
def compositor_perf(ctx, max_regression=0.15, rounds=2):
    """tools/kwin_pipeline_run.py while scrolling the drawer: KWin paint and SurfaceFlinger present intervals,
    CPU and GPU. Fails when paint p95 or the present interval p95 is worse than the previous release's by
    more than max_regression."""
    import subprocess
    out_dir = ctx['out_dir'] / 'compositor'
    run_ = subprocess.run(['uv', 'run', '--script', str(rungic_device.WORKSPACE / 'tools/kwin_pipeline_run.py'),
                           str(out_dir), '--rounds', str(rounds), '--seconds', '8', '--swipes', '6',
                           '--max-thermal', '2'], capture_output=True, text=True, timeout=1800)
    summary_path = out_dir / 'summary.json'
    if not summary_path.exists():
        return result(False, error=(run_.stderr or run_.stdout)[-1500:])
    summary = json.loads(summary_path.read_text())
    metrics = {'kwin_paint_ms_p95': summary.get('kwin_paint_ms_p95'), 'sf_interval_ms_p95': summary.get('sf_interval_ms_p95'),
               'kwin_cpu_pct': (summary.get('cpu_core_pct') or {}).get('kwin'),
               'plasmashell_cpu_pct': (summary.get('cpu_core_pct') or {}).get('plasmashell'),
               'gpu_busy_pct': summary.get('gpu_busy_pct')}
    regressions = []
    base = ctx.get('previous_metrics', {}).get('perf.compositor', {})
    for key in ('kwin_paint_ms_p95', 'sf_interval_ms_p95'):
        if isinstance(base.get(key), (int, float)) and isinstance(metrics[key], (int, float)) and base[key] > 0:
            if metrics[key] > base[key] * (1 + max_regression):
                regressions.append(f'{key} {base[key]} -> {metrics[key]}')
    return result(not regressions and metrics['kwin_paint_ms_p95'] is not None, metrics,
                  regressions=regressions, compared_with=base or None, summary=str(summary_path))


# ---------------------------------------------------------------- runner

def load():
    return json.loads(SCENARIOS.read_text())


def previous_report(release, scenario_ids):
    """Newest report of another release, for metric comparison."""
    candidates = []
    for report in RESULTS.glob('*/*/report.json'):
        if report.parent.parent.name != (release or 'unreleased'):
            candidates.append(report)
    for report in sorted(candidates, key=lambda p: p.parent.name, reverse=True):
        data = json.loads(report.read_text())
        if any(s['id'] in scenario_ids for s in data['scenarios']):
            return report, data
    return None, None


def compare(current, previous):
    rows = []
    old = {s['id']: s for s in previous['scenarios']}
    for scenario in current['scenarios']:
        before = old.get(scenario['id'])
        if not before:
            continue
        for key, value in scenario.get('metrics', {}).items():
            base = before.get('metrics', {}).get(key)
            if isinstance(value, (int, float)) and isinstance(base, (int, float)) and base:
                rows.append({'scenario': scenario['id'], 'metric': key, 'previous': base, 'current': value,
                             'change': round((value - base) / abs(base), 3)})
    return rows


def bring_to_front(timeout=10):
    """The scenarios need the desktop in front (the camera, the microphone and text input follow it).
    A deploy on 2026-09-28 found Android's launcher there, left by an earlier test, and failed on it."""
    apk = rungic_device.apk()
    top = lambda: apk in run('dumpsys activity activities | grep -m1 topResumedActivity', 'shell', 30, check=False).stdout
    was = top()
    if not was:
        run(f'am start -n {apk}/.MainActivity', 'shell', 30, check=False)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not top():
            time.sleep(0.5)
        time.sleep(2)   # the desktop's own visibility follows (capture, focus)
    return {'was_in_front': was, 'in_front': top()}


def write_report(path, report, initial=False):
    """Publish a complete JSON snapshot atomically. A new run cannot replace an existing report."""
    import os
    import tempfile
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.report-', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(report, stream, indent=1, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            if initial:
                os.link(temporary, path)  # exclusive creation, including simultaneous starts
            else:
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def report_summary(report):
    rows = report['scenarios']
    manual = report.get('manual_results', [])
    report['passed'] = bool(rows) and all(r['passed'] is True or
                                        r.get('details', {}).get('explicit_scope_exclusion') for r in rows)
    report['complete'] = all(r['passed'] is not None for r in rows)
    statuses = [r['status'] for r in rows]
    report['counts'] = {status: statuses.count(status) for status in ('pass', 'fail', 'skipped', 'unimplemented', 'not-run')}
    report['counts'].update({f'manual-{status}': sum(r['status'] == status for r in manual)
                             for status in ('pass', 'fail', 'not-run')})
    if 'fail' in statuses or any(r['status'] == 'fail' for r in manual):
        report['verdict'] = 'fail'
    elif not rows or report.get('state') != 'finished' or report.get('metadata_errors') or \
            any(status != 'pass' for status in statuses) or any(r['status'] != 'pass' for r in manual):
        report['verdict'] = 'incomplete'
    else:
        report['verdict'] = 'pass'


def device_snapshot():
    """Read the selected phone before the app enters the foreground. Keep the specified device state."""
    text = lambda command: (run(command, 'shell', timeout=15, check=True).stdout or '').strip()
    battery = text('dumpsys battery')
    powered = re.findall(r'(?:AC|USB|Wireless|Dock) powered:\s*(true|false)', battery, re.I)
    level = re.search(r'\blevel:\s*(\d+)', battery)
    power = text('dumpsys power')
    wake = re.search(r'mWakefulness=(\w+)', power)
    device = {'serial': text('getprop ro.serialno') or None, 'fingerprint': text('getprop ro.build.fingerprint') or None,
            'battery': {'charging': any(v.lower() == 'true' for v in powered) if powered else None,
                        'level': int(level.group(1)) if level else None},
            'screen': wake.group(1) if wake else None}
    missing = [name for name, value in {**device, **device['battery']}.items() if value is None]
    if missing:
        device['errors'] = ['unavailable fields: ' + ', '.join(missing)]
    return device


def run_scenarios(selected, release=None, out_dir=None, since=None, skips=None, scope='selected', manual_results=None, retry_of=None):
    if not selected:
        raise ValueError('no scenarios selected')
    skips = skips or {}
    unknown = set(skips) - {s['id'] for s in selected}
    if unknown:
        raise ValueError(f'skip IDs are not selected scenarios: {sorted(unknown)}')
    if len({s['id'] for s in selected}) != len(selected):
        raise ValueError('scenario IDs must be unique')
    spec = load()
    manual = [{'id': f'manual.{i}', 'title': title, 'status': 'not-run', 'note': ''}
              for i, title in enumerate(spec.get('manual', []), 1)] if scope == 'full' else []
    for id, value in (manual_results or {}).items():
        row = next((r for r in manual if r['id'] == id), None)
        if row is None:
            raise ValueError(f'manual ID is not in the {scope} plan: {id}')
        if value.get('status') not in ('pass', 'fail') or not value.get('note', '').strip():
            raise ValueError(f'manual result requires pass or fail and an observation: {id}')
        row.update(status=value['status'], note=value['note'].strip())
    started = time.time()
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    out_dir = Path(out_dir) if out_dir else RESULTS / (release or 'unreleased') / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / 'report.json'
    rows = [{'id': s['id'], 'title': s['title'], 'level': s['level'], 'status': 'not-run',
             'passed': None, 'metrics': {}, 'details': {}, 'seconds': 0} for s in selected]
    report = {'release': release, 'time': datetime.datetime.now().astimezone().isoformat(timespec='seconds'), 'scope': scope, 'state': 'running', 'front': {},
              'system': {}, 'device': {}, 'scenarios': rows, 'manual': spec.get('manual', []),
              'manual_results': manual, 'path': str(path)}
    if retry_of:
        report['retry_of'] = retry_of

    def save(initial=False):
        report_summary(report)
        report['skipped_ids'] = [r['id'] for r in rows if r['status'] in ('skipped', 'unimplemented')]
        report['failed_ids'] = [r['id'] for r in rows if r['passed'] is False]
        write_report(path, report, initial)

    save(initial=True)  # the full plan exists before the first device operation
    try:
        import rungic_release
        reference = rungic_release.git('rev-parse', '--verify', 'HEAD^{commit}')
        report['source'] = {'commit': reference, 'dirty': bool(rungic_release.git('status', '--porcelain'))}
        for key, capture in [('system', lambda: rungic_release.phone_drift(reference)), ('device', device_snapshot)]:
            try:
                report[key] = capture()
                if report[key].get('errors'):
                    report.setdefault('metadata_errors', {})[key] = '; '.join(report[key]['errors'])
            except (Exception, SystemExit) as error:
                report.setdefault('metadata_errors', {})[key] = f'{type(error).__name__}: {error}'
        report['system']['against'] = reference
        _, base = previous_report(release, {s['id'] for s in selected})
        ctx = {'spec': spec, 'since': since or started, 'release': release, 'out_dir': out_dir,
               'previous_metrics': {s['id']: s.get('metrics', {}) for s in (base or {}).get('scenarios', [])}}
        report['front'] = ctx['front'] = bring_to_front()
        save()
        for i, scenario in enumerate(selected):
            fn = CHECKS.get(scenario['check'])
            began = time.monotonic()
            if scenario['id'] in skips:
                row = {'passed': None, 'status': 'skipped', 'metrics': {},
                       'details': {'skipped': skips[scenario['id']], 'explicit_scope_exclusion': True}}
            elif fn is None:
                row = {'passed': None, 'status': 'unimplemented', 'metrics': {},
                       'details': {'skipped': 'check not implemented'}}
            else:
                try:
                    row = fn(ctx, **scenario.get('params', {}))
                except Exception as error:
                    row = result(False, error=f'{type(error).__name__}: {error}', trace=traceback.format_exc()[-1500:])
                row['status'] = 'pass' if row['passed'] is True else 'fail' if row['passed'] is False else 'unimplemented'
            if row['passed'] is False and scenario.get('screenshot_on_failure', True):
                try:
                    shot = Path(rungic_agent.screenshot())
                    target = out_dir / f"{scenario['id']}.png"
                    shot.replace(target)
                    row['details']['screenshot'] = str(target)
                except Exception:
                    pass
            rows[i] = {'id': scenario['id'], 'title': scenario['title'], 'level': scenario['level'], **row,
                       'seconds': round(time.monotonic() - began, 1)}
            save()
            print(f"{row['status'].upper()} {scenario['id']} ({rows[i]['seconds']} s)", flush=True)
        base_path, base = previous_report(release, {r['id'] for r in rows})
        if base:
            report['compared_with'] = str(base_path.relative_to(rungic_device.WORKSPACE))
            report['metric_changes'] = compare(report, base)
        report['state'] = 'finished'
        save()
        return report
    except BaseException as error:
        report['state'] = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'stopped'
        report['run_error'] = f'{type(error).__name__}: {error}'
        save()
        raise


def run_level(level, release=None, out_dir=None, since=None, skips=None, manual_results=None):
    levels = {'smoke': {'smoke'}, 'full': {'smoke', 'full'}}[level]
    return run_scenarios([s for s in load()['scenarios'] if s['level'] in levels], release, out_dir, since, skips,
                         scope=level, manual_results=manual_results)


def scenario_status(row):
    """Read old reports conservatively without modifying the original evidence."""
    if row.get('status'):
        return row['status']
    if row.get('passed') is True:
        return 'pass'
    if row.get('passed') is False:
        return 'fail'
    return 'skipped' if row.get('details', {}).get('explicit_scope_exclusion') else 'unimplemented'


def render_report(path):
    """A device-free, human-readable view of saved observations and the existing feature inventory."""
    from feature_inventory import Inventory
    path = Path(path).resolve()
    catalog = Inventory(rungic_device.WORKSPACE, files=[])
    plan = load()
    definitions = {s['id']: s for s in plan['scenarios']}
    areas = {a['id']: a['title'] for a in catalog.areas}
    attempts, warnings, seen = [], [], set()
    current = path
    while current:
        if current in seen:
            warnings.append('首次报告关联形成循环：' + str(current))
            break
        seen.add(current)
        try:
            report = json.loads(current.read_text())
        except (OSError, ValueError) as error:
            reason = '文件不存在' if isinstance(error, FileNotFoundError) else '文件无法读取或 JSON 无效'
            warnings.append(f'首次报告缺失（{current}）：{reason}')
            if attempts:
                attempts.insert(0, (current, {'missing': True, 'scenarios': []}))
            break
        attempts.insert(0, (current, report))
        current = (current.parent / report['retry_of']).resolve() if report.get('retry_of') else None
    if not attempts:
        raise ValueError(f'cannot read report: {path}')
    first, latest = attempts[0][1], attempts[-1][1]
    initial_missing = first.get('missing', False)
    scope = first.get('scope', 'unknown')
    scope_name = {'smoke': '部署后冒烟', 'full': '完整检查', 'selected': '已选检查'}.get(scope, '范围未记录')
    device, system = latest.get('device', {}), latest.get('system', {})
    actual = system.get('release')
    serial = device.get('serial')
    def md(value):
        if value is None or value == '':
            return '未知'
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        return str(value).replace('<', '&lt;').replace('>', '&gt;').replace('|', r'\|').replace('\n', '<br>')
    def status(row):
        if row is None:
            return ''
        value = scenario_status(row)
        if value == 'skipped':
            return '跳过：' + md(row.get('details', {}).get('skipped', '未记录原因'))
        return {'pass': '✓ 通过', 'fail': '✗ 失败', 'not-run': '未执行',
                'unimplemented': '检查未实现'}.get(value, md(value))
    def count(report):
        states = [scenario_status(r) for r in report.get('scenarios', [])]
        return {s: states.count(s) for s in ('pass', 'fail', 'skipped', 'unimplemented', 'not-run')}
    observed = {r['id']: r for _, report in attempts for r in report.get('scenarios', [])}
    counts = count({'scenarios': list(observed.values())})
    manual = first.get('manual_results', [])
    pending = sum(r['status'] == 'not-run' for r in manual)
    human_failed = sum(r['status'] == 'fail' for r in manual)
    missing = sum(counts[s] for s in ('skipped', 'unimplemented', 'not-run'))
    flaky = [id for id, row in observed.items() if scenario_status(row) == 'pass' and
             any(scenario_status(r) == 'fail' for _, report in attempts
                 for r in report.get('scenarios', []) if r['id'] == id)]
    failed = counts['fail'] + human_failed
    incomplete = missing or pending or warnings or not observed or scope == 'unknown' or \
        latest.get('state', 'unknown') != 'finished' or any(r.get('metadata_errors') for _, r in attempts)
    verdict = 'incomplete' if initial_missing else 'fail' if failed else 'incomplete' if incomplete else 'pass'
    if initial_missing:
        conclusion = '**未完成：首次报告缺失，无法确认其余检查的结果。**'
    elif failed:
        conclusion = '**未通过。** ' + '、'.join(text for n, text in [(counts['fail'], f'{counts["fail"]} 项自动检查失败'), (human_failed, f'{human_failed} 项人工失败')] if n) + '。'
    elif latest.get('state', 'unknown') != 'finished':
        conclusion = f'**未完成。** 运行中断或完成状态未知。计划 {len(observed)} 项，已有 {counts["pass"] + counts["fail"]} 项结果。'
    elif incomplete:
        reasons = []
        if missing:
            reasons.append(f'{missing} 项没有结果')
        if pending:
            reasons.append(f'人工 {pending} 项未填写结果')
        if any(r.get('metadata_errors') for _, r in attempts):
            reasons.append('现场信息采集不完整')
        if scope == 'unknown':
            reasons.append('原检查范围未记录')
        if not observed:
            reasons.append('没有已知检查计划')
        if warnings and not reasons:
            reasons.append('首次观察的关联不完整')
        conclusion = '**未完成。** ' + '。'.join(reasons) + '。'
    elif scope == 'full':
        conclusion = f'**本次完整检查计划通过。** 自动 {len(observed)} 项、人工 {len(manual)} 项通过。'
    else:
        conclusion = (f'**自动冒烟检查通过（{counts["pass"]}/{len(observed)}）。** 这是部署后的快速检查，不代表完整检查。'
                      if scope == 'smoke' else f'**所选检查通过（{counts["pass"]}/{len(observed)}）。**')
    if flaky:
        conclusion += f' 其中 {len(flaky)} 项重试才通过（不稳定）。首次失败的记录已保留。'
    if missing or pending:
        gaps = [(counts['skipped'], '跳过'), (counts['unimplemented'], '检查未实现'), (counts['not-run'], '未执行'), (pending, '待人工')]
        conclusion += ' 另有' + '、'.join(f'{n} 项{term}' for n, term in gaps if n) + '。'
    if scope == 'full':
        conclusion += ' 不包含首次安装、整机重启、长时间待机等生命周期测试。'
    elif not initial_missing:
        conclusion += f' 本次不包含需要真人检查的 {len(first.get("manual", []))} 项。'
    screen = device.get('screen')
    screen_warning = screen is not None and screen != 'Awake'
    if screen_warning:
        warnings.append(f'现场异常：开始时手机屏幕没有亮（{screen}）。依赖屏幕的检查可能因此失败。失败不一定来自版本本身。')
    if system.get('in_sync') is False:
        warnings.append(f'手机上的版本与 {str(system.get("against", "未知"))[:12]} 不同（{len(system.get("differs", []))} 处差异）。结论只适用于实际安装的内容。')
    for _, report in attempts:
        if report.get('run_error'):
            warnings.append('运行停止：' + report['run_error'])
        for key, error in report.get('metadata_errors', {}).items():
            warnings.append(f'未能读取 {key}：{error}')
    if not device or not system or not actual or not serial:
        warnings.append('部分手机身份或安装身份未知。报告没有用默认值代替。')
    if latest.get('source', {}).get('dirty'):
        warnings.append('生成报告时工作区有未提交的改动。')

    def features_for(id):
        return [catalog.features[ref.split('/')[0]] for ref in definitions.get(id, {}).get('covers', [])
                if '/' in ref and ref.split('/')[0] in catalog.features]
    def label(id):
        features = features_for(id)
        if not features:
            return definitions.get(id, {}).get('title', observed.get(id, {}).get('title', id)) + '（没有关联到任何用户功能）'
        return '。'.join(f'{areas.get(f["area"], f["area"])} › '
                         f'{catalog.scenarios.get(f["scenario"], {}).get("title", f["scenario"])} › {f["title"]}' for f in features)
    live_scenarios = {f['scenario'] for f in catalog.features.values() if f.get('status') != 'retired'}
    defined_coverage = {f['scenario'] for id in definitions for f in features_for(id)} & live_scenarios
    run_coverage = {f['scenario'] for id in observed for f in features_for(id)} & live_scenarios
    uncovered = live_scenarios - defined_coverage
    def counts_text(report):
        return '、'.join(f'{n} 项{term}' for term, n in zip(
            ('通过', '失败', '跳过', '检查未实现', '未执行'), count(report).values()) if n) or '没有执行结果'
    def attempt_name(i):
        return '首次' if i == 0 else '重试' if i == 1 else f'重试 {i}'
    observation_text = '。'.join(f'{attempt_name(i)}：报告缺失，计划未知' if r.get('missing') else
        f'{attempt_name(i)}（计划 {len(r.get("scenarios", []))} 项）：{counts_text(r)}'
        for i, (_, r) in enumerate(attempts))
    lines = [f'# 验收报告：{md(serial)} · {md(actual)} · {scope_name}', '', '> ' + conclusion, '']
    lines += ['> ⚠ ' + md(w) for w in warnings]
    lines += ['', '| 你想知道的 | 回答 |', '| --- | --- |',
              f'| 测的是什么 | {md(serial)} 上实际安装的 {md(actual)}。对比提交 {md(str(system.get("against", "未知"))[:12])} |',
              f'| 结果如何 | {observation_text} |',
              f'| 没测到什么 | 本次计划涉及 {len(run_coverage)}/{len(live_scenarios)} 个用户场景。验收计划没有直接检查的场景 {len(uncovered)} 个。人工检查未执行 {pending if scope == "full" else len(first.get("manual", []))} 项 |', '']
    if failed and screen_warning:
        next_step = '亮屏解锁后，在同一安装上重新运行冒烟检查，不需要重新部署。'
    elif failed:
        next_step = f'处理第 3 节的 {failed} 项失败。'
    elif latest.get('state') != 'finished':
        next_step = '重新运行检查。本报告保留了中断前的结果。'
    elif pending:
        next_step = f'补填 {pending} 项人工结果：`--manual ID=pass|fail:说明`。'
    elif missing:
        next_step = '补跑被跳过或未执行的检查，并补齐尚未实现的检查。'
    elif scope == 'smoke':
        next_step = '如果要发布，运行完整检查（full）。'
    else:
        next_step = ''
    if next_step:
        lines += ['**下一步：** ' + next_step, '']
    def yes_no(value, yes, no):
        return yes if value is True else no if value is False else '未知'
    def short_sha(value):
        return md(str(value)[:12]) if value else '未知'
    def recorded_time(value):
        try:
            time = datetime.datetime.fromisoformat(value)
            return time if time.tzinfo else None
        except (ValueError, TypeError):
            return None
    def display_time(value):
        time = recorded_time(value)
        if not time:
            return md(value) + '（时区未记录）'
        offset = time.strftime('%z')
        return time.strftime('%Y-%m-%d %H:%M:%S') + f'（UTC{offset[:3]}:{offset[3:]}）'
    battery = device.get('battery', {})
    apk = system.get('apk', {})
    if isinstance(apk, dict):
        apk = f'{md(apk.get("name"))}（{md(apk.get("version_code"))}）'
    difference = '、'.join(str(r.get('part', '未知部分')) for r in system.get('differs', []))
    source = latest.get('source', {})
    lines += ['## 1. 测的是哪台手机、哪个版本', '', '| 项目 | 记录 |', '| --- | --- |',
              f'| 手机 | {md(serial)} · 固件 {md(device.get("fingerprint"))} |',
              f'| 现场 | 屏幕 {md(screen)} · 电量 {md(battery.get("level"))}% · {yes_no(battery.get("charging"), "充电中", "未充电")} · {yes_no(latest.get("front", {}).get("in_front"), "Rungic 应用在前台", "Rungic 应用未在前台")} |',
              f'| 实际安装 | {md(actual)} · 提交 {short_sha(system.get("installed_commit", system.get("commit")))} · APK {apk} · 开发覆盖 {md(system.get("overlays"))} 项 |',
              f'| 与提交对比 | {short_sha(system.get("against"))} · {yes_no(system.get("in_sync"), "一致", "不一致")} · ' + (f'{len(system["differs"])} 处差异：{md(difference) if difference else "无"}' if 'differs' in system else '差异未记录') + ' |',
              f'| 报告来源 | 源码 {short_sha(source.get("commit"))}（{yes_no(source.get("dirty"), "有未提交改动", "工作区干净")}） · 时间 {display_time(latest.get("time"))} |', '']
    history_path = rungic_device.WORKSPACE / 'release/history.json'
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    prior = [r for r in history if actual and serial and r.get('version') == actual and r.get('serial') == serial]
    def earlier(entry):
        try:
            return recorded_time(entry['time']) < recorded_time(latest['time'])
        except (KeyError, ValueError, TypeError):
            return False  # cannot assert the order of a record without its timestamp
    prior = [r for r in prior if earlier(r)]
    if prior:
        lines += ['同一版本、同一手机的部署记录（不能据此认定安装内容相同）：', '']
        lines += [f'- {display_time(r.get("time"))}：{md(r.get("result"))}' + (f'。重试通过项 {md(r["flaky"])}' if r.get('flaky') else '') for r in prior]
        lines += ['']
    lines += ['## 2. 这次检查了什么，关联哪些用户能力', '',
              '关联不表示整条体验要求已经验证。', '',
              '- 真机实测：程序在手机上操作，并判断结果。',
              '- 接口检查：程序通过约定的接口或探针执行检查。每项实际做了什么，见该行的描述。',
              '- 人工：需要人看、听或亲手操作。', '',
              '| 本次实际检查 | 关联的用户能力 | 方式 | ' + ' | '.join(attempt_name(i) for i in range(len(attempts))) + ' |',
              '| --- | --- | --- | ' + ' | '.join('---' for _ in attempts) + ' |']
    interface_ids = []
    for id in observed:
        covers = definitions.get(id, {}).get('covers', [])
        if definitions.get(id, {}).get('check') == 'interface_contract':
            interface_ids.append(id)
            continue
        values = ['缺失' if report.get('missing') else status(next((r for r in report.get('scenarios', []) if r['id'] == id), None)) for _, report in attempts]
        lines.append(f'| {md(definitions.get(id, {}).get("title", observed[id].get("title", id)))} | {md(label(id))} | 真机实测 | ' + ' | '.join(values) + ' |')
    if interface_ids:
        lines += ['', f'**安卓与 Linux 之间的连接（{len(interface_ids)} 项，接口检查）**', '']
        for id in interface_ids:
            ref = next((r.removeprefix('iface:') for r in definitions[id].get('covers', []) if r.startswith('iface:')), definitions[id].get('params', {}).get('interface', '未知'))
            consumers = [f['title'] for f in catalog.features.values() if f.get('status') != 'retired' and ref in f.get('interfaces', [])]
            values = ['缺失' if report.get('missing') else status(next((r for r in report.get('scenarios', []) if r['id'] == id), None)) for _, report in attempts]
            lines.append(f'- {md(definitions[id].get("title", id))}（{md(catalog.interfaces.get(ref, {}).get("title", ref))}）：' + ' → '.join(values) +
                         f'。依赖功能 {len(consumers)} 项：{md("、".join(consumers[:3]))}')
    lines += ['', '## 3. 失败与重试', '']
    problems = [id for id in observed if any(scenario_status(r) != 'pass' for _, report in attempts
                for r in report.get('scenarios', []) if r['id'] == id)]
    for id in problems:
        prefix = '⚠ 重试通过（不稳定）' if id in flaky else status(observed[id])
        lines += [f'### {prefix}：{md(definitions.get(id, {}).get("title", observed[id].get("title", id)))}', '', '- 关联的用户能力：' + md(label(id))]
        for ref in definitions.get(id, {}).get('covers', []):
            fid, _, eid = ref.partition('/')
            for requirement in catalog.features.get(fid, {}).get('experience', []):
                if requirement['id'] == eid:
                    lines.append('- 关联要求：' + md(requirement['text']))
        for i, (source, report) in enumerate(attempts):
            for row in report.get('scenarios', []):
                if row['id'] == id:
                    lines.append(f'- {attempt_name(i)}：{status(row)}')
                    for key, value in row.get('details', {}).items():
                        lines.append(f'  - {"观察" if key == "error" else md(key)}：{md(value)}')
                    if row.get('metrics'):
                        lines.append('  - 指标：' + md(row['metrics']))
                    lines.append('  - 原始文件：' + md(source))
        lines += ['']
    for row in manual:
        if row['status'] == 'fail':
            lines += [f'- 人工失败：{md(row["title"])}（{md(row["id"])}） · {md(row["note"])}', '']
    if not problems and not human_failed:
        lines += ['没有记录到失败或缺失的自动结果。', '']
    lines += ['## 4. 本次没有覆盖的内容', '', '以下内容不能由本报告证明正常。', '']
    selected_ids = set(observed)
    omitted = [s for s in plan['scenarios'] if s['id'] not in selected_ids]
    lines += [f'**验收计划中本次未选择的自动检查（{len(omitted)} 项）：**', '']
    lines += ['- ' + md(s['title']) + '。关联的用户能力：' + md(label(s['id'])) for s in omitted] or ['无。']
    lines += ['', '**人工项：**', '']
    if scope == 'full':
        lines += [f'- {md(r["title"])}：' + ('待人工' if r['status'] == 'not-run' else status(r)) +
                  f'（{md(r["id"])}） · {md(r["note"])}' for r in manual] or ['无。']
    else:
        lines += ['- 本次不检查：' + md(title) for title in first.get('manual', [])] or ['无。']
    lines += ['', f'**验收计划里没有任何检查直接对应的用户场景（{len(uncovered)}/{len(live_scenarios)}）：**', '']
    for sid in sorted(uncovered):
        scenario = catalog.scenarios.get(sid, {})
        lines.append(f'- {md(areas.get(scenario.get("area")))} › {md(scenario.get("title", sid))}')
    if not uncovered:
        lines.append('没有发现完全未关联检查的场景。关联不表示所有体验要求都已验证。')
    lines += ['', '## 5. 指标变化', '']
    for source, report in attempts:
        for row in report.get('scenarios', []):
            if row['id'] == 'perf.compositor' and not row.get('details', {}).get('compared_with'):
                lines.append(f'- 合成器性能：无参考，未比较。已采集指标：{md(row.get("metrics"))}。来源 {md(source)}。')
    for source, report in attempts:
        if report.get('metric_changes'):
            lines += [f'- 报告 {md(source)}：{md(report["metric_changes"])}。参考报告 {md(report.get("compared_with"))}']
            reference = rungic_device.WORKSPACE / (report.get('compared_with') or '')
            try:
                reference_device = json.loads(reference.read_text()).get('device', {})
                same = all(reference_device.get(k) is not None and reference_device.get(k) == report.get('device', {}).get(k) for k in ('serial', 'fingerprint'))
                lines += [f'- 参考手机／固件：{md(reference_device.get("serial"))} / {md(reference_device.get("fingerprint"))}。' + ('身份相同。其他测试条件仍需核对。' if same else '不是已确认的同一现场，变化仅供参考。')]
            except (OSError, ValueError):
                lines += ['- 参考现场未知，不能视为同条件的性能回归证据。']
    if not any(r.get('metric_changes') for _, r in attempts):
        lines.append('本次没有记录可比较的指标变化。')
    lines += ['', '## 附录：检查明细', '', '| 原始文件 | 检查 ID | 状态 | 用时（秒） | 证据 |', '| --- | --- | --- | --- | --- |']
    for source, report in attempts:
        for row in report.get('scenarios', []):
            lines.append(f'| {md(source)} | {md(row["id"])} | {status(row)} | {md(row.get("seconds"))} | {md(row.get("details", {}).get("screenshot"))} |')
    for i, (source, report) in enumerate(attempts):
        lines += ['', f'{attempt_name(i)}报告自身（{md(source)}）：verdict={md(report.get("verdict"))}，state={md(report.get("state"))}。{counts_text(report)}。', '']
    verdict_word = {'pass': '通过', 'fail': '未通过', 'incomplete': '未完成'}[verdict]
    lines += [f'本页合并结论：{verdict_word}。功能说明来自当前仓库的 release/acceptance.json 与 quality/，不补写原始报告。', '']
    for source, report in attempts:
        if not report.get('missing'):
            lines += [f'原始环境（{md(source)}）：{md(report.get("system"))}。{md(report.get("device"))}。源码 {md(report.get("source"))}', '']
    output = path.with_name('report.md')
    output.write_text('\n'.join(lines), encoding='utf-8')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('smoke', 'full'):
        p = sub.add_parser(name); p.add_argument('--release')
        if name == 'full':
            p.add_argument('--manual', action='append', default=[], metavar='ID=pass|fail:OBSERVATION',
                           help='record actual human observations for manual.1, manual.2, ... from the plan')
        p.add_argument('--skip', action='append', default=[], metavar='ID=REASON',
                       help='record an explicit scope exclusion; never reported as PASS')
    p = sub.add_parser('run'); p.add_argument('ids', nargs='+'); p.add_argument('--release')
    p = sub.add_parser('compare'); p.add_argument('a'); p.add_argument('b')
    p = sub.add_parser('render'); p.add_argument('report')
    a = parser.parse_args()
    if a.cmd == 'render':
        print(render_report(a.report))
        return 0
    if a.cmd == 'compare':
        print(json.dumps(compare(json.loads(Path(a.b).read_text()), json.loads(Path(a.a).read_text())), indent=1))
        return 0
    if a.cmd == 'run':
        scenarios = load()['scenarios']
        unknown = set(a.ids) - {s['id'] for s in scenarios}
        if unknown:
            parser.error(f'unknown scenario IDs: {sorted(unknown)}')
        chosen = [s for s in scenarios if s['id'] in a.ids]
        if not chosen:
            parser.error('no scenarios selected')
        report = run_scenarios(chosen, a.release)
    else:
        skips = {}
        for entry in a.skip:
            key, sep, reason = entry.partition('=')
            if not sep or not reason.strip():
                parser.error('--skip requires ID=REASON')
            skips[key] = reason
        manual = {}
        for entry in getattr(a, 'manual', []):
            key, sep, observation = entry.partition('=')
            status, colon, note = observation.partition(':')
            if not sep or not colon or status not in ('pass', 'fail') or not note.strip():
                parser.error('--manual requires ID=pass|fail:OBSERVATION')
            if key in manual:
                parser.error(f'duplicate manual ID: {key}')
            manual[key] = {'status': status, 'note': note.strip()}
        try:
            report = run_level(a.cmd, a.release, skips=skips, manual_results=manual)
        except ValueError as error:
            parser.error(str(error))
    print(json.dumps({k: report[k] for k in ('verdict', 'passed', 'complete', 'failed_ids', 'path')}, ensure_ascii=False))
    return {'pass': 0, 'fail': 1, 'incomplete': 2}[report['verdict']]


if __name__ == '__main__':
    raise SystemExit(main())
