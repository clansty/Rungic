# SPDX-License-Identifier: GPL-2.0-or-later
"""The agent's work in flight, and the user's control of it (docs/114).

One owner for the three things every way of talking to the agent needs (push-to-talk, a call, the
director): give a running task more words, stop it, and keep both through a restart.

- Words for a task (steer) go into an inbox kept on disk first. Delivery: into the turn that runs on
  the task's thread (turn/steer, bound to that turn by expectedTurnId); when that turn has just
  ended, a new turn on the thread with the words (the work goes on with them). A word that cannot
  go now (Codex restarting) stays in the inbox and goes at the thread's next turn end, or when the
  service starts again. Words are never lost (2026-10-05: a correction that met the end of a turn
  was refused, "start a follow-up task", and the voice did not).
- Stop is a verb with a check: the turn is interrupted and stop is "done" only when Codex says the
  turn ended (turn/completed, or thread/read). Stop also empties the thread's inbox: words saved
  for it must not start the work again.
- Work in flight is written down (thread, turn, what kind of work, its workspace). Work still written
  down when the service starts was cut by the restart: the service continues it (VoiceAgent.resume_work).

Idea from firstmate (github.com/kunchenguid/firstmate): a durable steering inbox per task, and lifecycle
commands as a separate plane with a verified postcondition.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

# Work cut longer ago than this is not continued by itself: the user is told and says "continue".
RESUME_S = 30 * 60
# An inbox record older than this is dropped when the service starts (its thread is gone or done).
INBOX_KEPT_S = 24 * 3600
STOP_WAIT_S = 10.0
SETTLE_S = 3.0
RETRY_S = 5.0


class TaskControl:
    def __init__(self, server, path, describe=lambda thread: None, params=lambda thread: {'threadId': thread},
                 log=print, clock=time.time):
        """server() -> the Codex app-server connection (call(method, params, timeout)) or None.
        describe(thread) -> what work runs on `thread` ({'kind': 'main' | 'side' | 'read', 'conversation',
        'task', 'workspace'}) or None for threads that are not the user's work (curation).
        params(thread) -> the parameters of a new turn there (model, effort)."""
        self.server, self.describe, self.params, self.log, self.clock = server, describe, params, log, clock
        self.path = Path(path)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.inbox: list[dict] = []
        self.work: dict[str, dict] = {}       # thread -> the user's work running there (kept on disk)
        self.turns: dict[str, str] = {}       # thread -> its running turn (every thread)
        self.thread_locks: dict[str, threading.Lock] = {}
        self.retries: dict[str, threading.Timer] = {}
        self.saved_at = 0.0
        self.cut = self._load()

    # ---- the file ----------------------------------------------------------------------------
    def _load(self) -> list[dict]:
        """-> the work cut by the last end of the service."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        now = self.clock()
        self.inbox = [r for r in data.get('inbox') or [] if isinstance(r, dict) and r.get('thread')
                      and now - float(r.get('created') or 0) < INBOX_KEPT_S]
        cut = [dict(w, thread=t) for t, w in (data.get('work') or {}).items() if isinstance(w, dict)]
        self._save()
        return cut

    def _save(self) -> None:
        self.saved_at = self.clock()
        data = {'inbox': self.inbox, 'work': self.work}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False))
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError as error:
            self.log('task control: not saved', error)

    # ---- what Codex says -----------------------------------------------------------------------
    def on_notification(self, method: str, params: dict) -> None:
        thread = params.get('threadId') or ''
        if not thread:
            return
        deliver = False
        with self.lock:
            if method == 'turn/started':
                turn = (params.get('turn') or {}).get('id') or ''
                self.turns[thread] = turn
                info = self.describe(thread)
                if info:
                    self.work[thread] = {**info, 'turn': turn, 'started': self.clock(), 'seen': self.clock()}
                    self._save()
                self.changed.notify_all()
            elif method == 'turn/completed':
                turn = (params.get('turn') or {}).get('id') or ''
                if self.turns.get(thread) in (turn, None, ''):
                    self.turns.pop(thread, None)
                    if self.work.pop(thread, None) is not None:
                        self._save()
                deliver = any(r['thread'] == thread for r in self.inbox)
                self.changed.notify_all()
            elif thread in self.work:
                # Still at work: when the service last saw it (a cut is continued only if recent).
                self.work[thread]['seen'] = self.clock()
                if self.clock() - self.saved_at > 15:
                    self._save()
        if deliver:
            threading.Thread(target=self.deliver, args=(thread,), daemon=True).start()

    def running(self, thread: str) -> str | None:
        with self.lock:
            return self.turns.get(thread) or None

    def started(self, thread: str, turn: str) -> None:
        """A turn this service started (turn/start's reply may come before turn/started)."""
        with self.lock:
            if turn and thread not in self.turns:
                self.turns[thread] = turn
                self.changed.notify_all()

    # ---- words for a task (the data plane) -----------------------------------------------------
    def pending(self, thread: str | None = None) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.inbox if thread is None or r['thread'] == thread]

    def steer(self, thread: str, items: list[dict], source: str = '', durable: bool = True) -> dict:
        """Give the work on `thread` these input items. -> {'outcome': 'steered' (into the running turn) |
        'started' (a new turn, none was running) | 'queued' (kept; goes later), 'turn': id}.
        durable=False (a new task the caller tracks itself): nothing is kept when it cannot go now; the
        error is raised."""
        record = {'id': f'steer-{uuid.uuid4().hex}', 'thread': thread, 'input': list(items), 'source': source,
                  'created': self.clock()}
        with self.lock:
            self.inbox.append(record)
            if durable:
                self._save()
        result = self.deliver(thread)
        if not durable and result.get('outcome') == 'queued':
            with self.lock:
                self.inbox = [r for r in self.inbox if r['id'] != record['id']]
            raise RuntimeError(result.get('error') or 'The task connection is not ready')
        return result

    def deliver(self, thread: str) -> dict:
        """Send what waits in the inbox for `thread`, in order."""
        with self.lock:
            gate = self.thread_locks.setdefault(thread, threading.Lock())
        with gate:
            records = self.pending(thread)
            if not records:
                return {'outcome': 'none'}
            server = self.server()
            if server is None:
                self._retry(thread)
                return {'outcome': 'queued', 'error': 'The task connection is not ready'}
            try:
                turn = self.running(thread)
                if turn:
                    try:
                        self._steer(server, thread, turn, records)
                        return {'outcome': 'steered', 'turn': turn}
                    except RuntimeError as error:
                        # The turn ended or changed meanwhile: wait for Codex to say so, then go on.
                        self.log('task control: steer', thread, error)
                        turn = self._settle(thread, turn)
                        records = self.pending(thread)
                        if not records:
                            return {'outcome': 'steered', 'turn': turn}
                        if turn:
                            self._steer(server, thread, turn, records)
                            return {'outcome': 'steered', 'turn': turn}
                items = [item for record in records for item in record['input']]
                params = {**self.params(thread), 'threadId': thread, 'input': items,
                          'clientUserMessageId': records[0]['id']}
                reply = server.call('turn/start', params)
                turn = ((reply or {}).get('turn') or {}).get('id') or ''
                self.started(thread, turn)
                self._drop(records)
                return {'outcome': 'started', 'turn': turn}
            except Exception as error:  # noqa: BLE001 - kept in the inbox, tried again
                self.log('task control: not delivered', thread, error)
                self._retry(thread)
                return {'outcome': 'queued', 'error': str(error)}

    def _steer(self, server, thread, turn, records) -> None:
        for record in records:
            server.call('turn/steer', {'threadId': thread, 'expectedTurnId': turn, 'input': record['input'],
                                       'clientUserMessageId': record['id']})
            self._drop([record])

    def _drop(self, records) -> None:
        ids = {r['id'] for r in records}
        with self.lock:
            self.inbox = [r for r in self.inbox if r['id'] not in ids]
            self._save()

    def _settle(self, thread: str, old: str) -> str | None:
        """Wait until Codex says the turn `old` ended or another began -> the running turn or None."""
        deadline = self.clock() + SETTLE_S
        with self.lock:
            while self.turns.get(thread) == old and self.clock() < deadline:
                self.changed.wait(0.1)
            if self.turns.get(thread) != old:
                return self.turns.get(thread) or None
        # Codex did not say: ask it (not holding the lock: notifications go on meanwhile).
        status = self._turn_status(thread, old)
        with self.lock:
            if status and status != 'inProgress' and self.turns.get(thread) == old:
                self.turns.pop(thread, None)
            return self.turns.get(thread) or None

    def _retry(self, thread: str) -> None:
        with self.lock:
            if thread in self.retries:
                return
            timer = threading.Timer(RETRY_S, self._retry_now, (thread,))
            timer.daemon = True
            self.retries[thread] = timer
        timer.start()

    def _retry_now(self, thread: str) -> None:
        with self.lock:
            self.retries.pop(thread, None)
            attempts = 0
            for record in self.inbox:
                if record['thread'] == thread:
                    record['attempts'] = attempts = record.get('attempts', 0) + 1
        # Two minutes of tries; then the record waits for the thread's next turn end or a restart.
        if 0 < attempts <= 24:
            self.deliver(thread)

    def flush(self) -> None:
        """Deliver every waiting record (the service started, Codex is back)."""
        for thread in {r['thread'] for r in self.pending()}:
            self.deliver(thread)

    # ---- stop (the control plane) ------------------------------------------------------------
    def stop(self, thread: str, timeout: float = STOP_WAIT_S) -> dict:
        """Interrupt the turn on `thread` and wait until Codex says it ended. -> {'stopped': bool, 'turn'}.
        The thread's waiting words are dropped: a stop is not to start the work again."""
        with self.lock:
            dropped = [r for r in self.inbox if r['thread'] == thread]
            if dropped:
                self.inbox = [r for r in self.inbox if r['thread'] != thread]
                self._save()
        turn = self.running(thread)
        if not turn:
            return {'stopped': True, 'turn': '', 'dropped': len(dropped)}
        server = self.server()
        for _attempt in range(2):
            if server is not None:
                try:
                    server.call('turn/interrupt', {'threadId': thread, 'turnId': turn}, timeout=10)
                except Exception as error:  # noqa: BLE001 - checked below
                    self.log('task control: interrupt', thread, error)
            if self._ended(thread, turn, timeout):
                return {'stopped': True, 'turn': turn, 'dropped': len(dropped)}
            status = self._turn_status(thread, turn)
            if status and status != 'inProgress':
                with self.lock:
                    if self.turns.get(thread) == turn:
                        self.turns.pop(thread, None)
                        self.work.pop(thread, None)
                        self._save()
                    self.changed.notify_all()
                return {'stopped': True, 'turn': turn, 'dropped': len(dropped)}
        return {'stopped': False, 'turn': turn, 'dropped': len(dropped)}

    def _ended(self, thread: str, turn: str, timeout: float) -> bool:
        deadline = self.clock() + timeout
        with self.lock:
            while self.turns.get(thread) == turn and self.clock() < deadline:
                self.changed.wait(0.2)
            return self.turns.get(thread) != turn

    def _turn_status(self, thread: str, turn: str) -> str | None:
        """The status of `turn` as the thread's record has it (thread/read), or None."""
        server = self.server()
        if server is None:
            return None
        try:
            data = server.call('thread/read', {'threadId': thread, 'includeTurns': True}, timeout=15)
        except Exception as error:  # noqa: BLE001
            self.log('task control: thread/read', thread, error)
            return None
        record = (data or {}).get('thread') or {}
        if (record.get('status') or {}).get('type') not in (None, 'active'):
            # Not loaded or idle: no turn runs there, whatever the record of the last one says.
            return 'interrupted'
        for item in record.get('turns') or []:
            if item.get('id') == turn:
                return item.get('status')
        return None

    # ---- work cut by a restart ---------------------------------------------------------------
    def take_cut(self) -> list[dict]:
        """The work the last end of the service cut, each with 'recent' (continue it by itself). Once."""
        cut, self.cut = self.cut, []
        now = self.clock()
        for record in cut:
            record['recent'] = now - float(record.get('seen') or record.get('started') or 0) < RESUME_S
        return cut
