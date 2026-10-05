# SPDX-License-Identifier: GPL-2.0-or-later
"""Offline: inspect native transport settings without a phone, key or server."""
import importlib.util
from pathlib import Path
import sys
import threading

MODULE = Path(__file__).resolve().parents[2] / 'agent/assistant/phone_session.py'
sys.path.insert(0, str(MODULE.parent))      # task_state, as the service finds it
spec = importlib.util.spec_from_file_location('phone_session', MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def bridge(settings=None):
    obj = module.PhoneSession.__new__(module.PhoneSession)
    obj.settings = lambda: settings or {'sandbox': 'danger-full-access', 'config': {
        'mcp_servers.rungic-desktop.env': {'WAYLAND_DISPLAY': 'agent-0'}}}
    obj.lock = threading.RLock()
    obj.threads = {}
    obj.cards, obj.card_timers = {}, {}
    obj.executor, obj.shared, obj.side_slots = None, {}, {}
    obj.emit = lambda event, keep=True: None
    obj.history = lambda conversation: []
    return obj


# covers: agent.phone-mode/E2
def test_read_tasks_disable_all_configured_mcp(tmp_path, monkeypatch):
    (tmp_path / '.codex').mkdir()
    (tmp_path / '.codex/config.toml').write_text('[mcp_servers.writes]\ncommand="unsafe"\n')
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    settings = bridge()._task_settings('task1', True)
    assert settings['sandbox'] == 'read-only'
    assert settings['config']['mcp_servers.writes.enabled'] is False
    assert settings['config']['mcp_servers.rungic-desktop.enabled'] is False


# covers: agent.phone-mode/E2
def test_exclusive_tools_are_task_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    obj = bridge()
    config = obj._task_settings('task1', False)['config']
    assert config['mcp_servers.rungic-desktop.command'] == 'rungic-task-tools'
    assert config['mcp_servers.rungic-desktop.args'] == ['--task', 'task1', '--', 'rungic-cua', 'mcp']
    assert config['mcp_servers.rungic-desktop.env']['RUNGIC_TASK_ID'] == 'task1'
    assert config['mcp_servers.rungic-desktop.env']['WAYLAND_DISPLAY'] == 'agent-0'
    assert 'RUNGIC_TASK_ID' not in obj.settings()['config']['mcp_servers.rungic-desktop.env']


def test_rpc_strips_private_fields_and_registers_thread_before_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    calls, replies = [], []
    class Server:
        def call(self, method, params):
            calls.append((method, params))
            return {'thread': {'id': 'codex-thread'}}
    obj = bridge()
    obj.server = lambda: Server()
    def reply(message):
        assert obj.owns('codex-thread')
        replies.append(message)
    obj._write = reply
    obj._rpc({'id': 1, 'method': 'thread/start', 'params': {'taskId': 'task1', 'readOnly': True, 'conversation': 'origin'}})
    assert not {'taskId', 'readOnly', 'conversation'} & calls[0][1].keys()
    assert obj.threads['codex-thread']['conversation'] == 'origin'
    assert replies[0]['result']['thread']['id'] == 'codex-thread'


# covers: agent.phone-mode/E7
def test_notifications_stay_scoped():
    obj = bridge()
    obj.threads['native-thread'] = {'conversation': 'origin'}
    messages = []
    obj._write = messages.append
    assert not obj.notification('turn/completed', {'threadId': 'selected-chat'})
    assert obj.notification('turn/completed', {'threadId': 'native-thread'})
    assert len(messages) == 1


# covers: agent.phone-mode/E11
def test_desktop_language_does_not_force_spoken_language():
    obj = bridge()
    obj.foreground = lambda: True
    obj.prompt = lambda: 'Speak the user\'s language'
    obj.language = lambda: 'en'
    obj.command = lambda method, args: args
    assert obj.start('origin')['language'] == ''


class Process:
    """The coordinator's side of the pipe: the lines it printed, then gone."""
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False
    def poll(self):
        return 0 if self.terminated else None
    def terminate(self):
        self.terminated = True
    def wait(self, timeout=None):
        return 0


def reader(lines):
    obj = bridge()
    obj.process = Process(lines)
    obj.pending = {}
    obj.snapshot = {'sessionId': 's', 'phase': 'live', 'conversation': 'c'}
    obj.events = []
    obj.emit = lambda event, keep=True: obj.events.append(event)
    return obj


# covers: agent.phone-mode/E1
def test_a_reply_without_an_id_is_dropped_not_the_end():
    # ExternalBusy was written without an id; its reply had none and the reader's KeyError took
    # the coordinator down with it (2026-10-03).
    import json
    event, result = threading.Event(), {}
    obj = reader([json.dumps({'type': 'reply', 'result': {'ok': True}}) + '\n',
                  'not json\n',
                  json.dumps({'type': 'reply', 'id': 7, 'result': {'sessionId': 'x'}}) + '\n',
                  json.dumps({'type': 'event', 'event': {'type': 'phone-notice', 'text': 'still here'}}) + '\n'])
    obj.pending[7] = event, result
    obj._read()
    # Answered by the reply after the bad lines (the coordinator stopping at the end marks what is
    # left pending, which command() would have taken already).
    assert event.is_set() and result['sessionId'] == 'x'
    assert {'type': 'phone-notice', 'text': 'still here'} in obj.events


# covers: agent.phone-mode/E1
def test_post_gives_every_command_an_id():
    obj = bridge()
    obj.serial = 0
    written = []
    obj._write = written.append
    obj.post('ExternalBusy', {'busy': True})
    obj.post('ExternalBusy', {'busy': False})
    assert [m['id'] for m in written] == [1, 2]
    assert all(m['type'] == 'command' and m['method'] == 'ExternalBusy' for m in written)


# covers: agent.phone-mode/E1
def test_post_to_a_stopped_coordinator_is_dropped():
    obj = bridge()
    obj.serial = 0
    def stopped(message):
        raise RuntimeError('Phone session service stopped; try again')
    obj._write = stopped
    obj.post('ExternalBusy', {'busy': True})   # a hint: no exception into the agent's turn handling


# covers: agent.phone-mode/E1
def test_the_agent_writes_no_command_without_an_id():
    # Every command to the coordinator goes through command() or post(), which number it.
    source = (MODULE.parent / 'rungic_voice_agent.py').read_text()
    assert 'phone._write(' not in source


# covers: agent.phone-mode/E4
def test_an_open_call_keeps_the_phone_awake(tmp_path, monkeypatch):
    # A call goes on with the screen locked (2026-10-05): while it is open the session's process is
    # named in rungic-call.busy, which rungic-agent-wakelock holds the phone awake for.
    import json
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    obj = reader([json.dumps({'type': 'event', 'event': {'type': 'phone-state', 'sessionId': 'voice_1'}}) + '\n'])
    obj.process.pid = 4242
    obj._read()
    marker = tmp_path / 'rungic-call.busy'
    assert json.loads(marker.read_text()) == {'pid': 4242}
    obj = reader([json.dumps({'type': 'event', 'event': {'type': 'phone-state', 'sessionId': ''}}) + '\n'])
    obj._read()
    assert not marker.exists()

# covers: agent.phone-mode/E17
def test_a_phone_task_is_tracked_as_a_turn():
    # The same task state as a push-to-talk turn (task_state.py): plan and commentary make the
    # task's card; the card as it ended stays with the task.
    import time as clock
    events = []
    obj = module.PhoneSession.__new__(module.PhoneSession)
    obj.lock = threading.RLock()
    obj.threads = {'th1': {'taskId': 'task_1', 'conversation': 'c1'}}
    obj.cards, obj.card_timers = {}, {}
    obj.emit = lambda event, keep=True: events.append((event, keep))
    posted = []
    obj.post = lambda method, args=None: posted.append((method, args))
    obj._track('turn/started', {'threadId': 'th1'})
    obj._track('turn/plan/updated', {'threadId': 'th1', 'plan': [{'step': 'Write the brief', 'status': 'inProgress'}]})
    obj._track('item/completed', {'threadId': 'th1', 'item': {'type': 'agentMessage', 'id': 'm1', 'phase': 'commentary',
                                                              'text': 'Next I draw the tiles.'}})
    clock.sleep(0.6)
    live = [e for e, keep in events if e['type'] == 'task' and not keep]
    assert live and live[-1]['taskId'] == 'task_1' and live[-1]['conversation'] == 'c1'
    assert live[-1]['plan'] == [{'step': 'Write the brief', 'status': 'inProgress'}]
    assert posted and posted[-1][0] == 'TaskFacts' and 'In progress: Write the brief' in posted[-1][1]['facts'], \
        'the voice is told the same facts as in push-to-talk'
    obj._track('turn/completed', {'threadId': 'th1', 'turn': {'status': 'completed'}})
    final = [e for e, keep in events if e.get('final')]
    assert final and final[-1]['plan'] and 'current' not in final[-1]
    obj._track('turn/plan/updated', {'threadId': 'other', 'plan': []})
    assert len(events) == len(live) + 1, 'a thread that is not a phone task is left alone'


class Executor:
    """VoiceAgent as a call's executor: the conversation's own thread and its running turn."""
    def __init__(self, running=None):
        self.running = running
        self.opened = []

    def main_thread(self, conversation):
        self.opened.append(conversation)
        return 'main-thread'

    def running_turn(self, thread):
        return self.running

    def turn_params(self, thread):
        return {'threadId': thread, 'model': 'gpt-x'}

    def main_workspace(self):
        return 1

    def workspace_settings(self, slot):
        return {'RUNGIC_WORKSPACE': str(slot), 'WAYLAND_DISPLAY': f'wayland-ws-{slot}'}


class Server:
    def __init__(self, fail_start=False):
        self.calls, self.fail_start = [], fail_start

    def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        if method == 'turn/start' and self.fail_start:
            raise RuntimeError('a turn is active')
        if method == 'turn/start':
            return {'turn': {'id': 'turn-new'}}
        return {}


def shared_bridge(executor, server):
    obj = bridge()
    obj.executor, obj.shared = executor, {}
    obj.server = lambda: server
    written = []
    obj._write = written.append
    return obj, written


# covers: agent.phone-mode/E7
def test_work_that_acts_goes_to_the_conversation_s_own_thread():
    # 2026-10-05: a call's tasks each had a thread of their own, queued behind one another; push-to-
    # talk joins the turn at work. Now a call's acting work is push-to-talk's: the same thread.
    executor, server = Executor(), Server()
    obj, written = shared_bridge(executor, server)
    obj._rpc({'id': 1, 'method': 'thread/start', 'params': {'taskId': 't1', 'readOnly': False, 'conversation': 'c1'}})
    assert written[-1]['result'] == {'thread': {'id': 'main-thread'}, 'shared': True}
    assert executor.opened == ['c1'] and not server.calls, 'no thread of its own'
    obj._rpc({'id': 2, 'method': 'turn/start', 'params': {'threadId': 'main-thread', 'input': [{'type': 'text', 'text': 'draw'}]}})
    assert server.calls[-1] == ('turn/start', {'threadId': 'main-thread', 'model': 'gpt-x', 'input': [{'type': 'text', 'text': 'draw'}]})
    assert written[-1]['result'] == {'turn': {'id': 'turn-new'}}
    # A second request while the turn works, with no workspace free for it, joins the turn, as
    # push-to-talk does (the cast and the drawing).
    executor.running = 'turn-new'
    obj._side_thread = lambda task, conversation: None
    obj._rpc({'id': 3, 'method': 'thread/start', 'params': {'taskId': 't2', 'readOnly': False, 'conversation': 'c1'}})
    obj._rpc({'id': 4, 'method': 'turn/start', 'params': {'threadId': 'main-thread', 'input': [{'type': 'text', 'text': 'cast it'}]}})
    assert server.calls[-1] == ('turn/steer', {'threadId': 'main-thread', 'expectedTurnId': 'turn-new',
                                               'input': [{'type': 'text', 'text': 'cast it'}]})
    assert written[-1]['result'] == {'turn': {'id': 'turn-new'}, 'joined': True}
    assert obj.shared_tasks('main-thread') == ['t1', 't2']


# covers: agent.phone-mode/E7
def test_a_turn_begun_meanwhile_is_joined():
    executor, server = Executor(), Server(fail_start=True)
    obj, written = shared_bridge(executor, server)
    obj._rpc({'id': 1, 'method': 'thread/start', 'params': {'taskId': 't1', 'readOnly': False, 'conversation': 'c1'}})
    executor.running = 'ptt-turn'     # push-to-talk began one between
    obj._rpc({'id': 2, 'method': 'turn/start', 'params': {'threadId': 'main-thread', 'input': []}})
    assert server.calls[-1][0] == 'turn/steer' and written[-1]['result']['turn']['id'] == 'ptt-turn'


# covers: agent.phone-mode/E2
def test_read_only_work_runs_beside_in_a_thread_of_its_own():
    executor, server = Executor(), Server()
    obj, written = shared_bridge(executor, server)
    server.call = lambda method, params, timeout=None: server.calls.append((method, params)) or {'thread': {'id': 'side'}}
    obj._rpc({'id': 1, 'method': 'thread/start', 'params': {'taskId': 't3', 'readOnly': True, 'conversation': 'c1'}})
    method, params = server.calls[-1]
    assert method == 'thread/start' and params['sandbox'] == 'read-only'
    assert 'This task is read-only' in params['developerInstructions'], 'it says so rather than "I cannot"'
    assert obj.owns('side') and not executor.opened


# covers: agent.phone-mode/E7
def test_the_conversation_s_thread_stays_push_to_talk_s():
    executor, server = Executor(), Server()
    obj, written = shared_bridge(executor, server)
    obj._rpc({'id': 1, 'method': 'thread/start', 'params': {'taskId': 't1', 'readOnly': False, 'conversation': 'c1'}})
    assert obj.notification('turn/completed', {'threadId': 'main-thread'}) is False, 'its card, approvals and progress'
    assert written[-1] == {'type': 'notification', 'method': 'turn/completed', 'params': {'threadId': 'main-thread'}}
    assert obj.request(9, 'item/tool/requestUserInput', {'threadId': 'main-thread'}) is True, 'a task question is the call\'s'
    assert obj.request(10, 'item/commandExecution/requestApproval', {'threadId': 'main-thread'}) is False, 'approvals stay cards'


# covers: agent.phone-mode/E7
def test_the_voice_agent_s_executor_methods_are_not_shadowed():
    # 2026-10-05: VoiceAgent.turn (its TurnState, often None) shadowed an executor method named
    # turn, and a call's task failed with "'NoneType' object is not callable".
    import ast, re
    source = (MODULE.parent / 'rungic_voice_agent.py').read_text()
    agent = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'VoiceAgent')
    methods = {n.name for n in agent.body if isinstance(n, ast.FunctionDef)}
    used = set(re.findall(r'self\.executor\.([a-z_]+)\(', MODULE.read_text()))
    assert used and used <= methods, used - methods
    for name in used:
        assert not re.search(rf'\bself\.{name}\s*=(?!=)', source), f'VoiceAgent assigns self.{name}'



# covers: agent.phone-mode/E2
def test_a_new_job_while_work_runs_goes_on_at_the_same_time(monkeypatch):
    # 2026-10-05, the user: music in Ardour while the drawing in Krita goes on, "make it parallel":
    # a thread of its own, in a free workspace of its own, given back when it ends.
    import types
    claims, released = [], []
    fake = types.SimpleNamespace(claim=lambda record, exclude=(): claims.append((record, exclude)) or 2,
                                 release=lambda slot, pid: released.append(slot))
    monkeypatch.setitem(sys.modules, 'rungic_cua', types.SimpleNamespace(workspace=fake))
    monkeypatch.setitem(sys.modules, 'rungic_cua.workspace', fake)
    executor, server = Executor(running='turn-draw'), Server()
    obj, written = shared_bridge(executor, server)
    obj.settings = lambda: {'sandbox': 'danger-full-access', 'config': {}, 'developerInstructions': 'agent.md'}
    obj.side_slots = {}
    server.call = lambda method, params, timeout=None: server.calls.append((method, params)) or {'thread': {'id': 'side-music'}}
    obj._rpc({'id': 1, 'method': 'thread/start', 'params': {'taskId': 'music', 'readOnly': False, 'conversation': 'c1'}})
    method, params = server.calls[-1]
    assert method == 'thread/start' and claims[0][1] == (1,), 'not the main work\'s workspace'
    assert params['config']['mcp_servers.rungic-desktop.env']['RUNGIC_WORKSPACE'] == '2'
    assert params['config']['shell_environment_policy.set']['WAYLAND_DISPLAY'] == 'wayland-ws-2'
    assert 'workspace 2' in params['developerInstructions']
    assert obj.owns('side-music') and 'shared' not in written[-1]['result'], 'its own card and lease'
    obj.notification('turn/completed', {'threadId': 'side-music', 'turn': {'id': 't', 'status': 'completed'}})
    assert released == [2], 'its workspace is free again'
