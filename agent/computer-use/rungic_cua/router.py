"""Where the agent works (docs/research/91): the user's desktop or its own workspace.

The screens beside the phone's own are two: desktop mode (the user's own desktop, a KWin of its own:
workspace 0, docs/research/97 §19, shown in a floating window or on the TV in computer mode) and the
assistant's screen (the agent's own workspace, a KWin of its own). The agent works:

- on the user's desktop while desktop mode is on or a TV shows the desktop: the user is at that
  screen and wants the work there;
- else in its own workspace;
- or where the user says (`desktop_where`), for the rest of the conversation.

Codex starts this server once per thread, a sub-agent's as its parent's, all with the parent's
environment (docs/research/91, "由 Codex 当组长"): a sub-agent's (its calls say thread_source
"subagent") takes a workspace of its own at its first desktop call (workspace.claim), works only
there, and gives it back when it closes it or ends. Agents working in parallel cannot share one
desktop: KWin has one pointer, one keyboard focus.

The voice agent starts this MCP server in the workspace's environment, with the user's session
as RUNGIC_USER_*. It serves the tools itself but acts through two children, `rungic-cua mcp` in
each session, started when first needed: each keeps all the logic of its session (windows,
input, screenshots, switching apps over), and nothing here touches a desktop.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import socket
import subprocess
import threading
import time
from itertools import count
from pathlib import Path

from . import hold, team, workspace

# Set apart in a workspace, as Plasma's desktop session has them (docs/103); the user's session's
# values go along as RUNGIC_USER_<name>.
USER_VALUES = ('PLASMA_INTEGRATION_USE_PORTAL', 'QT_QPA_PLATFORMTHEME')


logger = logging.getLogger('rungic-cua.router')

PLATFORM = os.environ.get('RUNGIC_PLATFORM_SOCKET', '/mnt/android-wayland/platform.sock')
WHERE_TOOL = {
    'name': 'desktop_where',
    'description': ("Where your desktop tools work. 'desktop': the user's desktop (desktop mode: workspace 0, "
                    "shown in its floating window or on the TV; the user watches and may use it too; turned on if "
                    "it is off). 'workspace': your own workspace (the assistant's screen), which the user's phone "
                    "never shows by itself. 'auto' (the default): the user's desktop while desktop mode is on or "
                    "the TV shows the desktop, else your workspace. Set it "
                    "when the user says where to work (\"on my desktop\", \"on the assistant's screen\", \"在我的桌面上\", "
                    "\"在助理屏上\"); it holds for this "
                    "conversation. Without `target` it only says where you work now and why, and which "
                    "workspace it is: start programs from your shell with `rungic-workspace-env N COMMAND` "
                    "(N 0 for the user's desktop) so that their windows open there (your shell's environment is "
                    "not the workspace's). A sub-agent gets a workspace of its own and works only there."),
    'inputSchema': {'type': 'object', 'properties': {
        'target': {'type': 'string', 'enum': ['auto', 'desktop', 'workspace']}}},
    'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False},
}
CLOSE_TOOL = {
    'name': 'desktop_close_workspace',
    'description': ("Close your workspace (the assistant's screen) when its work is done or the user asks: every "
                    "app in it is asked to close as with its close button, so save your work first. An app that "
                    "does not close (it asks about unsaved changes) comes back in `remaining` and the workspace "
                    "stays: handle it (save or discard as the user wants) and call again. `force` closes anyway "
                    "and loses what is unsaved: only when the user said so. Everything started there ends with it; "
                    "your next desktop tool call starts it again, empty. A sub-agent closes its workspace "
                    "when its part is done, which gives the workspace back for other agents."),
    'inputSchema': {'type': 'object', 'properties': {'force': {'type': 'boolean'}}},
    'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': False},
}
SESSION_NAMES = {'desktop': "the user's desktop", 'workspace': 'your workspace (the assistant\'s screen)'}


# The Android host frozen (the phone asleep, docs/research/97): one request that waited its whole
# timeout marks it; for a minute after, requests (of every process) fail at once instead of each
# waiting again, and one tries again after that.
UNREACHABLE_FOR_S = 60


def _unreachable_path() -> Path:
    return Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}') / 'rungic-host-unreachable'


def _recently_unreachable() -> bool:
    try:
        return time.time() - _unreachable_path().stat().st_mtime < UNREACHABLE_FOR_S
    except OSError:
        return False


def _note_reachable(reachable: bool) -> None:
    try:
        if reachable:
            _unreachable_path().unlink(missing_ok=True)
        else:
            _unreachable_path().touch()
    except OSError:
        pass


def bridge(request: dict, timeout: float = 3.0) -> dict:
    if _recently_unreachable():
        raise OSError(errno.EAGAIN, 'the Android host is not responding')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(PLATFORM)
            conn.sendall(json.dumps(request).encode() + b'\n')
            reply = b''
            while not reply.endswith(b'\n'):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                reply += chunk
    except OSError:
        _note_reachable(False)
        raise
    _note_reachable(True)
    return json.loads(reply)


def desktop_running() -> bool:
    """Desktop mode is on while the user's independent desktop (workspace 0) runs."""
    runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    return os.path.exists(os.path.join(runtime, 'wayland-ws-0'))


def desktop_in_use() -> tuple[bool, str]:
    """Whether the user has their desktop screen out: desktop mode on, or a TV showing it."""
    if desktop_running():
        return True, 'desktop mode is on'
    try:
        state = bridge({'op': 'desktop-mode'})
    except (OSError, ValueError) as error:
        return False, f'desktop mode is off (TV unknown: {error})'
    if state.get('tv'):
        return True, 'the TV shows the desktop'
    return False, 'desktop mode is off and no TV shows the desktop'


def user_session_env(env: dict) -> dict:
    """The user's session, from the workspace's environment (RUNGIC_USER_*, else its usual one)."""
    env = dict(env)
    runtime = env.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    env['WAYLAND_DISPLAY'] = env.get('RUNGIC_USER_WAYLAND_DISPLAY') or 'wayland-0'
    env['DBUS_SESSION_BUS_ADDRESS'] = env.get('RUNGIC_USER_DBUS_SESSION_BUS_ADDRESS') or f'unix:path={runtime}/bus'
    for name in ('RUNGIC_WORKSPACE', 'DISPLAY', 'XAUTHORITY', 'PULSE_SINK'):  # PULSE_SINK: the workspace's sound
        env.pop(name, None)
    # What the workspace sets as a desktop does (docs/103): the user's session's own values again.
    for name in USER_VALUES:
        value = env.pop('RUNGIC_USER_' + name, '')
        if value:
            env[name] = value
        else:
            env.pop(name, None)
    return env


class Child:
    """`rungic-cua mcp` in one session, spoken to over its stdio."""

    def __init__(self, env: dict) -> None:
        self.process = subprocess.Popen(['rungic-cua', 'mcp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        env={**env, 'RUNGIC_CUA_CHILD': '1'}, text=True, bufsize=1)
        self.ids = count(1)
        self.lock = threading.Lock()
        self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                    'clientInfo': {'name': 'rungic-cua-router', 'version': '0.1.0'}})
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def alive(self) -> bool:
        return self.process.poll() is None

    def _send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        with self.lock:
            rid = next(self.ids)
            self._send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
            while True:
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError('the desktop tools of that session stopped')
                message = json.loads(line)
                if message.get('id') == rid:
                    break
        if 'error' in message:
            raise RuntimeError(message['error'].get('message', 'error'))
        return message.get('result') or {}

    def close(self) -> None:
        try:
            self.process.stdin.close()
            self.process.wait(3)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()


def bus_identity(env: dict) -> int:
    """The session bus `env` talks to, as its socket's inode: a workspace started again has a new
    bus, and a child connected to the old one only answers "The connection is closed"."""
    address = env.get('DBUS_SESSION_BUS_ADDRESS', '')
    path = address.split('path=', 1)[1].split(',', 1)[0] if 'path=' in address else ''
    try:
        return os.stat(path).st_ino if path else 0
    except OSError:
        return 0


# A child whose bus went away (its workspace started again) answers with this.
STALE_BUS = 'The connection is closed'


def workspace_env(env: dict, slot: int, run=subprocess.run) -> dict:
    """`env` moved into workspace `slot`: what rungic-workspace-env sets (the user's session goes
    along as RUNGIC_USER_*, already in `env`)."""
    out = run(['rungic-workspace-env', str(slot), 'env', '-0'], capture_output=True, env=env, timeout=10, check=True)
    return dict(item.split('=', 1) for item in out.stdout.decode().split('\0') if '=' in item)


class Router:
    def __init__(self, env: dict | None = None) -> None:
        self.env = dict(env or os.environ)
        self.home = int(self.env.get('RUNGIC_WORKSPACE') or 1)   # the parent's workspace
        self.override = 'auto'
        self.children: dict[str, Child] = {}
        self.buses: dict[str, tuple] = {}    # each child's session bus: (address env, socket inode)
        self.last: str | None = None
        self.subagent: dict | None = None    # a sub-agent's thread: {'thread', 'parent'}
        self.claimed: int | None = None      # the workspace it holds
        self.member: dict | None = None      # its last team_post: role, kind, project (team.py)
        self.thread: str = ''                # its Codex thread (the murmur follows its log)
        self.murmur = None
        self.gate = hold.Gate()              # the user's take-overs of a screen (docs/114)

    def caller(self, meta: dict | None) -> None:
        """Who calls, from the first call's _meta (Codex 0.159: x-codex-turn-metadata)."""
        turn = (meta or {}).get('x-codex-turn-metadata') or {}
        self.thread = self.thread or turn.get('thread_id') or (meta or {}).get('threadId') or ''
        if self.subagent is None and turn.get('thread_source') == 'subagent':
            self.subagent = {'thread': turn.get('thread_id') or (meta or {}).get('threadId'),
                             'parent': turn.get('parent_thread_id')}
            logger.info('sub-agent %s of %s', self.subagent['thread'], self.subagent['parent'])

    @property
    def slot(self) -> int:
        """The workspace this thread works in: the parent's, or a sub-agent's own (taken now if needed)."""
        if not self.subagent:
            return self.home
        if self.claimed is not None:
            holder = workspace.claim_holder(self.claimed)
            if holder and holder.get('pid') == os.getpid():
                workspace.touch_claim(self.claimed)
                return self.claimed
            logger.warning('workspace %s: claim lost', self.claimed)
            self.drop('workspace')
        self.claimed = workspace.claim({'pid': os.getpid(), **self.subagent}, exclude=[self.home])
        if self.claimed is None:
            raise RuntimeError('every agent workspace is taken by other agents: wait until one is closed '
                               '(desktop_close_workspace gives it back) or work without the desktop')
        logger.info('sub-agent %s: workspace %s', self.subagent['thread'], self.claimed)
        return self.claimed

    def where(self) -> tuple[str, str]:
        """(target, why)."""
        if self.override != 'auto':
            return self.override, 'the user said so'
        if self.subagent:
            return 'workspace', 'a sub-agent works in a workspace of its own'
        in_use, why = desktop_in_use()
        return ('desktop' if in_use else 'workspace'), why

    def describe(self, target: str, why: str) -> dict:
        data = {'where': target, 'screen': SESSION_NAMES[target], 'why': why}
        if target == 'workspace':
            slot = self.slot
            data.update(workspace=slot, shell=f'rungic-workspace-env {slot} COMMAND')
            if self.subagent:
                data['yours'] = 'this workspace is yours alone; close it (desktop_close_workspace) when your part is done'
        else:
            # The user's desktop is workspace 0 (desktop mode, docs/research/97 §19).
            data.update(workspace=0, shell='rungic-workspace-env 0 COMMAND')
        return data

    def drop(self, target: str) -> None:
        child = self.children.pop(target, None)
        if child:
            child.close()

    def child(self, target: str) -> Child:
        child = self.children.get(target)
        bus = self.buses.get(target)
        if child is not None and child.alive() and bus and bus[1] and bus_identity(bus[0]) != bus[1]:
            # Its workspace started again (a new bus): the child's connection is to the old one.
            self.drop(target)
            child = None
        if child is None or not child.alive():
            if target == 'workspace':
                slot = self.slot
                if not workspace.ensure(slot):
                    why = workspace.failure(slot)
                    raise RuntimeError(f'workspace {slot} did not start' + (f': {why}' if why else ''))
                env = self.env if slot == self.home else workspace_env(self.env, slot)
            else:
                # The user's desktop: workspace 0, desktop mode turned on if it is off.
                if not desktop_running():
                    done = subprocess.run(['rungic-desktop-mode', 'on'], capture_output=True, text=True,
                                          env=user_session_env(self.env), timeout=60)
                    if not desktop_running():
                        raise RuntimeError('desktop mode did not start: ' + (done.stderr or done.stdout).strip()[-300:])
                env = workspace_env(self.env, 0)
            child = self.children[target] = Child(env)
            address = {'DBUS_SESSION_BUS_ADDRESS': env.get('DBUS_SESSION_BUS_ADDRESS', '')}
            self.buses[target] = (address, bus_identity(address))
        return child

    def call(self, name: str, arguments: dict, meta: dict | None = None) -> dict:
        """A tools/call result: from the child of the session the agent works in now."""
        self.caller(meta)
        self.follow_murmur()
        if name == WHERE_TOOL['name']:
            if arguments.get('target'):
                self.override = str(arguments['target'])
            target, why = self.where()
            self.last = target
            data = {**self.describe(target, why), 'setting': self.override}
            return {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}
        if name == team.TOOL['name']:
            return self.team_post(arguments)
        if name == CLOSE_TOOL['name']:
            # The child in the workspace goes with it; the next call starts both again.
            slot = self.slot
            news = self.gate.check(slot, name)
            if news:
                return {'content': [{'type': 'text', 'text': news}]}
            self.drop('workspace')
            data = workspace.close(slot, force=bool(arguments.get('force')))
            if self.subagent and data.get('closed'):
                team.clear(slot)
                workspace.release(slot, os.getpid())
                self.claimed = None
                self.last = None
            return {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}
        target, why = self.where()
        # The user took this screen over in the director (docs/114): acting waits for it, and the
        # agent hears once that the user gave it back.
        news = self.gate.check(self.slot if target == 'workspace' else 0, name)
        if news and name in hold.ACTING:
            return {'content': [{'type': 'text', 'text': news}]}
        if target == 'workspace':
            workspace.thaw(self.slot)
            self.at_work()
            self.follow_murmur()
        result = self.child(target).request('tools/call', {'name': name, 'arguments': arguments})
        if news:
            result = {**result, 'content': [*result.get('content', []), {'type': 'text', 'text': news}]}
        if result.get('isError') and any(STALE_BUS in str(c.get('text', '')) for c in result.get('content', [])):
            # A bus gone under the child: once more with a new child.
            self.drop(target)
            result = self.child(target).request('tools/call', {'name': name, 'arguments': arguments})
        if target != self.last:
            # The agent learns where it works whenever that changes.
            note = self.describe(target, why)
            result = {**result, 'content': [*result.get('content', []),
                                            {'type': 'text', 'text': json.dumps(note, ensure_ascii=False)}]}
            self.last = target
        return result

    def follow_murmur(self) -> None:
        """Once its workspace is known: say this thread's tool calls on its tile (team.Murmur)."""
        if self.murmur or not self.thread or (self.subagent and self.claimed is None):
            return
        self.murmur = team.Murmur(self.thread, self.claimed if self.subagent else self.home)

    def at_work(self) -> None:
        """A member acting after its review is at work: its tile says so without its words."""
        if self.subagent and self.member and self.member.get('kind') in ('review', 'brief') and self.claimed:
            self.member = {**self.member, 'kind': 'progress'}
            team.update_state({'role': self.member.get('role', ''), 'kind': 'progress', 'workspace': self.claimed})

    def team_post(self, arguments: dict) -> dict:
        """team_post (team.py): a member's post goes on its own workspace's tile, the lead's to the journal."""
        kind = str(arguments.get('kind') or 'progress')
        if kind not in team.KINDS:
            kind = 'progress'
        entry = {'role': str(arguments.get('role') or ''), 'kind': kind, 'text': str(arguments.get('text') or '')}
        if self.subagent:
            entry.update(workspace=self.slot, thread=self.subagent.get('thread'), parent=self.subagent.get('parent'))
        else:
            # The lead's own workspace: its tile is named by its role too.
            entry.update(workspace=self.home, thread=self.thread)
        project = str(arguments.get('project') or (self.member or {}).get('project') or '')
        self.member = {**entry, 'project': project}
        data = team.post(entry, project)
        return {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}

    def close(self) -> None:
        if self.murmur:
            self.murmur.close()
        for child in self.children.values():
            child.close()
        # A member that ends without saying how: "ended" for it, so its tile does not stay at work.
        if self.member and self.member.get('kind') not in team.FINAL:
            ended = {k: v for k, v in self.member.items() if k != 'project'}
            team.post({**ended, 'kind': 'ended', 'text': '', 'time': None}, self.member.get('project', ''))
        if self.claimed is not None:
            workspace.release(self.claimed, os.getpid())
