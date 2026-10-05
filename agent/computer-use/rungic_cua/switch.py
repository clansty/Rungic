"""Apps that run once per user's data, switched into the agent's workspace (docs/research/91).

The agent works in a workspace of its own. Most apps simply start a second instance there
(its own session bus: KDE's single-instance handling stays within it). A few cannot: one
instance per profile or login (WeChat, Chromium profiles, Telegram ...); a second
one fails on the profile lock or hands its request to the first, on the user's phone. Those are
switched: closed in the user's session - only after the user agreed (the tool refuses without
it) and never during a call - reopened in the workspace, and given back to the user's session
once the agent is done (`restore`, from the voice agent after a quiet period).
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

# Set apart in a workspace, as Plasma's desktop session has them (docs/103); the user's session's
# values go along as RUNGIC_USER_<name>.
USER_VALUES = ('PLASMA_INTEGRATION_USE_PORTAL', 'QT_QPA_PLATFORMTHEME')


# Program names (the executable's base name) of apps with one instance per user's data.
# Firefox is not one of them any more: in a workspace it has a profile of its own with a copy of the
# user's sign-ins (/usr/bin/firefox), and both always open (the user, 2026-10-05).
SINGLE_INSTANCE = {'wechat', 'chromium', 'chromium-browser', 'google-chrome',
                   'google-chrome-stable', 'telegram-desktop', 'telegram', 'thunderbird', 'signal-desktop'}
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
STATE = RUNTIME / 'rungic-workspace-switched.json'


def programs(entry: dict) -> set[str]:
    """Names its processes run under: the Exec program, and what that resolves to."""
    words = shlex.split(entry.get('exec') or '') or [entry.get('id', '')]
    names = {os.path.basename(words[0])}
    for word in words[:1]:
        for base in ('', *os.environ.get('PATH', '/usr/bin').split(':')):
            path = Path(base, word) if base else Path(word)
            if path.is_absolute() and path.exists():
                names.add(path.resolve().name)
                break
    return {n.casefold() for n in names if n}


def single_instance(entry: dict) -> bool:
    return bool(programs(entry) & SINGLE_INSTANCE)


def _process_names(pid: int) -> set[str]:
    """What a process is called: its executable, without a "-bin" suffix, and the program its
    command line names (Firefox runs /usr/lib/firefox/firefox-bin, started as .../firefox)."""
    names = set()
    try:
        exe = os.path.basename(os.readlink(f'/proc/{pid}/exe')).casefold()
        names |= {exe, exe.removesuffix('-bin')}
    except OSError:
        pass
    try:
        argv0 = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0', 1)[0].decode(errors='replace')
        if argv0.startswith('/') or ' ' not in argv0:     # not a retitled process ("Web Content")
            names.add(os.path.basename(argv0).casefold())
    except OSError:
        pass
    return {n for n in names if n}


def _parent(pid: int) -> int:
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('PPid:'):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def _display(pid: int) -> str | None:
    try:
        for item in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0'):
            if item.startswith(b'WAYLAND_DISPLAY='):
                return item[16:].decode(errors='replace')
    except OSError:
        return None
    return ''


def processes(names: set[str], workspace: int | None) -> list[int]:
    """This user's processes of `names`: in workspace `workspace`, or with None in the user's
    session (any display but a workspace's)."""
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        if not _process_names(pid) & names:
            continue
        # The app itself, not its helpers (a browser's content processes): ended one by one,
        # they left crashed tabs.
        if _process_names(_parent(pid)) & names:
            continue
        display = _display(pid)
        if display is None:
            continue
        in_workspace = display.startswith('wayland-ws-')
        if workspace is None and not in_workspace or workspace is not None and display == f'wayland-ws-{workspace}':
            found.append(pid)
    return found


def in_call(names: set[str]) -> bool:
    """Whether the app has a microphone open (a call, a meeting): never switched then."""
    try:
        out = subprocess.run(['pactl', '-f', 'json', 'list', 'source-outputs'], capture_output=True, text=True,
                             timeout=5).stdout
        streams = json.loads(out or '[]')
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    binaries = {str(s.get('properties', {}).get('application.process.binary', '')).casefold() for s in streams}
    return bool({b.removesuffix('-bin') for b in binaries} & names or binaries & names)


def _terminate(pids: list[int], timeout: float) -> list[int]:
    """SIGTERM, as a session ending does: browsers keep their session, messengers their data.
    Returns what is still running after `timeout`."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = [p for p in pids if Path(f'/proc/{p}').exists() and _process_names(p)]
        if not pids:
            break
        time.sleep(0.3)
    return pids


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False))


def close_in_user_session(entry: dict, pids: list[int]) -> list[int]:
    """Close the user's instance (the user agreed) and remember to give it back. Returns the
    processes that did not end."""
    left = _terminate(pids, 15)
    if not left:
        state = _load()
        state[entry['id']] = {'name': entry.get('name') or entry['id'], 'programs': sorted(programs(entry)),
                              'time': time.time(), 'workspace': int(os.environ.get('RUNGIC_WORKSPACE') or 1)}
        _save(state)
    return left


def switched() -> dict:
    return _load()


def restore(workspace: int) -> list[str]:
    """Give switched apps back: end them in the workspace, start them again in the user's
    session (a messenger then signs in by itself, a browser restores its session). An app in a
    call in the workspace is left for later. Returns the names given back."""
    state = _load()
    given = []
    for app_id, info in list(state.items()):
        # Each workspace gives back its own (switched before 2026-10-01: workspace 1, the only one).
        if int(info.get('workspace') or 1) != int(workspace):
            continue
        names = set(info.get('programs') or [])
        if in_call(names):
            continue
        if _terminate(processes(names, workspace), 15):
            continue            # still running: try again later
        env = dict(os.environ)
        if env.get('RUNGIC_USER_WAYLAND_DISPLAY') or env.get('RUNGIC_WORKSPACE'):
            env['WAYLAND_DISPLAY'] = env.get('RUNGIC_USER_WAYLAND_DISPLAY') or 'wayland-0'
            env['DBUS_SESSION_BUS_ADDRESS'] = (env.get('RUNGIC_USER_DBUS_SESSION_BUS_ADDRESS')
                                               or f'unix:path={RUNTIME}/bus')
        for name in ('RUNGIC_WORKSPACE', 'DISPLAY', 'XAUTHORITY', 'PULSE_SINK'):  # PULSE_SINK: the workspace's sound
            env.pop(name, None)
        # What the workspace sets as a desktop does (docs/103): the user's session's own values again.
        for name in USER_VALUES:
            value = env.pop('RUNGIC_USER_' + name, '')
            if value:
                env[name] = value
            else:
                env.pop(name, None)
        if not processes(names, None):
            subprocess.Popen(['kstart', '--application', app_id], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        del state[app_id]
        given.append(info.get('name') or app_id)
    _save(state)
    return given
