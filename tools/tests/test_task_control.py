# SPDX-License-Identifier: GPL-2.0-or-later
"""Offline: the user's control of the agent's work in flight (agent/assistant/task_control.py, docs/114):
words kept until they reach the task, stop with a check, work a restart cut."""
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agent/assistant'))
import task_control  # noqa: E402


class Server:
    """Codex app-server: records calls; `steer_fails` makes turn/steer fail (the turn ended)."""

    def __init__(self, steer_fails=False, interrupt=None, status='inProgress', active=True):
        self.calls, self.steer_fails, self.interrupt = [], steer_fails, interrupt
        self.status, self.active = status, active

    def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        if method == 'turn/steer' and self.steer_fails:
            raise RuntimeError('turn/steer: expected turn mismatch')
        if method == 'turn/start':
            return {'turn': {'id': 'turn-2'}}
        if method == 'turn/interrupt' and self.interrupt:
            self.interrupt()
        if method == 'thread/read':
            return {'thread': {'status': {'type': 'active' if self.active else 'notLoaded'},
                               'turns': [{'id': 'turn-1', 'status': self.status}]}}
        return {}


def control(tmp_path, server, **kw):
    return task_control.TaskControl(lambda: server, tmp_path / 'task-control.json', log=lambda *a: None,
                                    params=lambda thread: {'threadId': thread, 'model': 'gpt-x'}, **kw)


def text(words):
    return [{'type': 'text', 'text': words}]


# covers: agent.task-control/E2
def test_words_go_into_the_running_turn(tmp_path):
    server = Server()
    c = control(tmp_path, server)
    c.on_notification('turn/started', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    done = c.steer('th', text('make it red'), source='director')
    assert done == {'outcome': 'steered', 'turn': 'turn-1'}
    method, params = server.calls[-1]
    assert method == 'turn/steer' and params['expectedTurnId'] == 'turn-1' and params['input'] == text('make it red')
    assert not c.pending(), 'delivered: nothing kept'


# covers: agent.task-control/E2
def test_words_that_meet_the_end_of_the_turn_go_on_in_a_new_turn(tmp_path, monkeypatch):
    # 2026-10-05: a correction that met the end of a turn was refused ("start a follow-up task").
    monkeypatch.setattr(task_control, 'SETTLE_S', 2.0)
    server = Server(steer_fails=True)
    c = control(tmp_path, server)
    c.on_notification('turn/started', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    # Codex says the turn ended a moment after the refused steer.
    threading.Timer(0.2, c.on_notification, ('turn/completed', {'threadId': 'th', 'turn': {'id': 'turn-1'}})).start()
    done = c.steer('th', text('then cast it'))
    assert done == {'outcome': 'started', 'turn': 'turn-2'}
    method, params = server.calls[-1]
    assert method == 'turn/start' and params['input'] == text('then cast it') and params['model'] == 'gpt-x'
    assert params['clientUserMessageId'].startswith('steer-')
    assert c.running('th') == 'turn-2' and not c.pending()


# covers: agent.task-control/E2
def test_words_wait_on_disk_while_codex_is_away(tmp_path):
    away = control(tmp_path, None)
    away.server = lambda: None
    done = away.steer('th', text('use the blue one'))
    assert done['outcome'] == 'queued'
    for timer in away.retries.values():
        timer.cancel()
    # The service restarts: the words are still there and go.
    server = Server()
    again = control(tmp_path, server)
    assert [r['input'] for r in again.pending('th')] == [text('use the blue one')]
    again.flush()
    assert server.calls[-1][0] == 'turn/start' and server.calls[-1][1]['input'] == text('use the blue one')
    assert not again.pending()


# covers: agent.task-control/E2
def test_words_kept_go_when_the_turn_ends(tmp_path):
    server = Server()
    c = control(tmp_path, server)
    c.inbox.append({'id': 'steer-1', 'thread': 'th', 'input': text('and save it'), 'source': '', 'created': 0})
    c.inbox[-1]['created'] = c.clock()
    c.on_notification('turn/started', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    c.on_notification('turn/completed', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    for _ in range(50):
        if not c.pending():
            break
        threading.Event().wait(0.05)
    assert not c.pending() and server.calls[-1][0] == 'turn/start'


# covers: agent.task-control/E3
def test_stop_is_done_when_codex_says_the_turn_ended(tmp_path):
    c = control(tmp_path, None)
    server = Server(interrupt=lambda: threading.Timer(0.1, c.on_notification, (
        'turn/completed', {'threadId': 'th', 'turn': {'id': 'turn-1', 'status': 'interrupted'}})).start())
    c.server = lambda: server
    c.on_notification('turn/started', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    c.inbox.append({'id': 'steer-1', 'thread': 'th', 'input': text('more'), 'source': '', 'created': c.clock()})
    done = c.stop('th', timeout=2)
    assert done['stopped'] is True and done['dropped'] == 1, 'words kept for it do not start it again'
    assert ('turn/interrupt', {'threadId': 'th', 'turnId': 'turn-1'}) in server.calls
    assert not c.pending() and c.running('th') is None


# covers: agent.task-control/E3
def test_stop_not_confirmed_is_said(tmp_path):
    server = Server(status='inProgress', active=True)
    c = control(tmp_path, server)
    c.on_notification('turn/started', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    done = c.stop('th', timeout=0.2)
    assert done['stopped'] is False
    assert [m for m, _ in server.calls].count('turn/interrupt') == 2, 'asked twice'


# covers: agent.task-control/E3
def test_stop_confirmed_by_the_thread_record(tmp_path):
    server = Server(status='interrupted')
    c = control(tmp_path, server)
    c.on_notification('turn/started', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    assert c.stop('th', timeout=0.2)['stopped'] is True


# covers: agent.task-control/E4
def test_work_a_restart_cut_is_known_when_the_service_starts(tmp_path):
    kinds = {'main': {'kind': 'main', 'conversation': 'main', 'workspace': 1}}
    c = control(tmp_path, Server(), describe=kinds.get)
    c.on_notification('turn/started', {'threadId': 'main', 'turn': {'id': 'turn-1'}})
    c.on_notification('turn/started', {'threadId': 'curation', 'turn': {'id': 'turn-9'}})
    # The service ends here (killed): the next one finds the work it cut, not the curation.
    later = control(tmp_path, Server(), describe=kinds.get)
    cut = later.take_cut()
    assert [(w['thread'], w['kind'], w['turn'], w['recent']) for w in cut] == [('main', 'main', 'turn-1', True)]
    assert later.take_cut() == [], 'once'
    # Long ago: said, not continued by itself.
    old = control(tmp_path, Server(), describe=kinds.get, clock=lambda: 10 ** 10)
    old.cut = [dict(cut[0], seen=0)]
    assert old.take_cut()[0]['recent'] is False


# covers: agent.task-control/E4
def test_work_that_ended_is_not_cut(tmp_path):
    kinds = {'th': {'kind': 'side', 'conversation': 'c', 'workspace': 2}}
    c = control(tmp_path, Server(), describe=kinds.get)
    c.on_notification('turn/started', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    c.on_notification('turn/completed', {'threadId': 'th', 'turn': {'id': 'turn-1'}})
    assert control(tmp_path, Server(), describe=kinds.get).take_cut() == []
