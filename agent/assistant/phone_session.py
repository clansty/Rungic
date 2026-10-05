# SPDX-License-Identifier: GPL-2.0-or-later
"""Transport adapter for the native phone-session coordinator.

Audio and intent control live in C++. The work is push-to-talk's (2026-10-05, the user: a call is
only another way to talk; what it does must be the same): a task that may act goes to the
conversation's own Codex thread, the one push-to-talk and typing use, through `executor`; while a
turn runs there it joins it (turn/steer). Read-only tasks run beside it, each in a read-only thread
of its own (at most two, the C++ scheduler), as before. This adapter reuses the resident service's
authenticated Codex connection and conversation store.
"""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import tomllib

import task_state


class PhoneSession:
    def __init__(self, server, settings, emit, foreground, prompt, language, executable='rungic-agent-session',
                 history=lambda conversation: [], executor=None):
        self.server, self.settings, self.emit = server, settings, emit
        self.foreground, self.prompt, self.language = foreground, prompt, language
        self.history = history
        # The conversation's own thread and its running turn (VoiceAgent): main_thread(conversation)
        # -> thread id or None, running_turn(thread) -> turn id or None, turn_params(thread) -> the
        # parameters of a new turn there (model, effort), as push-to-talk and typing start one.
        self.executor = executor
        self.shared = {}            # the conversation's thread -> the C++ tasks that went to it
        self.side_slots = {}        # a parallel acting task's thread -> the workspace it holds
        self.side_home = {}         # ... and the workspace its settings put it in, held or not
        self.claim_touched = {}
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.pending = {}
        self.threads = {}
        # What each phone task is doing, kept as for a push-to-talk turn (task_state, docs/89): the
        # same plan, current activity and files, shown on the same task card.
        self.cards = {}
        self.card_timers = {}
        self.serial = 0
        self.snapshot = {'sessionId': '', 'phase': 'closed', 'tasks': [], 'conversation': ''}
        # Push-to-talk's session on the same coordinator (docs/115): its state is VoiceAgent's only.
        self.voice = {'sessionId': '', 'phase': 'closed', 'conversation': ''}
        self.process = subprocess.Popen(['sh', '-c', '[ ! -r /etc/profile.d/proxy.sh ] || . /etc/profile.d/proxy.sh; exec "$@"', 'rungic-phone-session', executable], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=None, text=True, bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _write(self, message):
        with self.write_lock:
            if self.process.poll() is not None:
                raise RuntimeError('Phone session service stopped; try again')
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
            self.process.stdin.flush()

    def alive(self):
        return self.process.poll() is None

    def post(self, method, args=None):
        """A command nobody waits for (ExternalBusy, a hint). It has an id all the same: the
        coordinator answers every command with its id, and a reply without one ended the reader and
        the coordinator with it (2026-10-03). A stopped coordinator drops the hint."""
        with self.lock:
            self.serial += 1
            rid = self.serial
        try:
            self._write({'type': 'command', 'id': rid, 'method': method, 'args': args or {}})
        except (RuntimeError, OSError):
            pass

    def command(self, method, args=None, timeout=30):
        with self.lock:
            self.serial += 1
            rid = self.serial
            event, result = threading.Event(), {}
            self.pending[rid] = event, result
        try:
            self._write({'type': 'command', 'id': rid, 'method': method, 'args': args or {}})
            if not event.wait(timeout):
                raise RuntimeError('Phone session did not answer')
            if 'error' in result:
                raise RuntimeError(str(result['error']))
            return result
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def start(self, conversation, mode='call', instructions=None):
        """A call (`mode` call) or push-to-talk's voice (press, docs/115) in `conversation`."""
        if mode == 'call' and not self.foreground():
            raise RuntimeError('Open Plasma before starting phone mode')
        return self.command('StartPhoneMode', {'conversationId': conversation, 'mode': mode,
                            # Desktop language is a reply fallback, not a forced
                            # transcription language: the user may speak another.
                            'instructions': (instructions or self.prompt()) + self.context(conversation), 'language': ''})

    def context(self, conversation):
        records = []
        for event in self.history(conversation)[-80:]:
            if event.get('type') in ('message', 'agent-message'):
                records.append({'role': event.get('role', 'assistant'), 'text': event.get('text', '')[:3000]})
            elif event.get('type') == 'phone-task':
                task = event.get('task') or {}
                if task.get('result'):
                    records.append({'role': 'task_result', 'text': task['result'][:3000]})
        while len(json.dumps(records, ensure_ascii=False)) > 16000:
            records.pop(0)
        return '\nRecent conversation (quoted context only; do not execute old requests):\n' + json.dumps(records[-20:], ensure_ascii=False)

    def owns(self, thread):
        with self.lock:
            return thread in self.threads

    def notification(self, method, params):
        thread = params.get('threadId')
        with self.lock:
            shared = thread in self.shared
        if shared:
            # The conversation's own thread: the coordinator follows its tasks' state, and the turn
            # is push-to-talk's all the same (its card, approvals, progress, errors): not claimed.
            try:
                self._write({'type': 'notification', 'method': method, 'params': params})
            except (RuntimeError, OSError):
                pass
            return False
        if not self.owns(thread):
            return False
        self._track(method, params)
        self._keep_claim(thread)
        if method == 'turn/completed':
            self._release_side(thread)     # its workspace is free again
        elif method == 'turn/started':
            self._reclaim_side(thread)     # words for it after its end: it works there again
        self._write({'type': 'notification', 'method': method, 'params': params})
        return True

    def shared_tasks(self, thread):
        """The coordinator's tasks that went to the conversation's own thread `thread`."""
        with self.lock:
            return sorted(self.shared.get(thread, ()))

    def _track(self, method, params):
        """Codex's notifications of a phone task into its TurnState, as the push-to-talk turn does."""
        thread = params.get('threadId')
        with self.lock:
            info = self.threads.get(thread)
            if not info:
                return
            if method == 'turn/started' or thread not in self.cards:
                self.cards[thread] = task_state.TurnState()
            state = self.cards[thread]
            changed = False
            if method == 'turn/plan/updated':
                state.on_plan(params.get('plan') or [], params.get('explanation'))
                changed = True
            elif method in ('item/started', 'item/completed'):
                item = params.get('item') or {}
                completed = method.endswith('completed')
                changed = state.on_item(item, completed)
                if completed and item.get('type') == 'agentMessage' and item.get('phase') != 'final_answer' and item.get('text'):
                    state.on_commentary(item['text'])
                    changed = True
            elif method == 'item/commandExecution/outputDelta':
                changed = state.on_output(params.get('itemId', ''), params.get('delta', ''))
            elif method == 'item/fileChange/patchUpdated':
                changed = state.on_patch(params.get('itemId', ''), params.get('changes') or [])
            elif method == 'turn/completed':
                # The card as it ended stays with the task (plan, files, steps), as for a turn.
                final = state.snapshot()
                final.pop('current', None)
                timer = self.card_timers.pop(thread, None)
                if timer:
                    timer.cancel()
        if method == 'turn/completed':
            self.emit({'type': 'task', 'taskId': info.get('taskId', ''), 'conversation': info.get('conversation', ''), 'final': True, **final})
            return
        if changed:
            self._card_changed(thread)

    def _card_changed(self, thread):
        """Tell the app, a few times a second at most (as VoiceAgent.task_changed)."""
        with self.lock:
            if thread in self.card_timers:
                return
            timer = threading.Timer(0.4, self._flush_card, (thread,))
            timer.daemon = True
            self.card_timers[thread] = timer
        timer.start()

    def _flush_card(self, thread):
        with self.lock:
            self.card_timers.pop(thread, None)
            info, state = self.threads.get(thread), self.cards.get(thread)
            snapshot = state.snapshot() if state else None
            facts = state.facts() if state else ''
        if info and snapshot is not None:
            self.emit({'type': 'task', 'taskId': info.get('taskId', ''), 'conversation': info.get('conversation', ''), **snapshot}, False)
            # What the voice may say of it, as push-to-talk's voice is told (TurnState.facts).
            if info.get('taskId'):
                self.post('TaskFacts', {'taskId': info['taskId'], 'facts': facts})

    def request(self, rid, method, params):
        thread = params.get('threadId')
        with self.lock:
            shared = thread in self.shared
        # On the conversation's own thread a task's question is the call's to ask (answer_task);
        # approvals stay push-to-talk's cards.
        if not self.owns(thread) and not (shared and method == 'item/tool/requestUserInput'):
            return False
        self._write({'type': 'request', 'id': rid, 'method': method, 'params': params})
        return True

    def _task_settings(self, task, read_only):
        settings = self.settings()
        settings['config'] = dict(settings.get('config') or {})
        config = settings['config']
        # Read-only tasks cannot call arbitrary MCP servers, including configured
        # third-party servers which may mutate outside the filesystem sandbox.
        names = {'rungic-desktop'}
        try:
            raw = tomllib.loads((Path.home() / '.codex/config.toml').read_text())
            names.update(raw.get('mcp_servers', {}))
        except FileNotFoundError:
            pass
        if read_only:
            settings['sandbox'] = 'read-only'
            for name in names:
                config[f'mcp_servers.{name}.enabled'] = False
            # agent.md speaks of full access and the desktop tools: this task has neither, and says
            # so plainly instead of "I cannot" (it runs beside the work, docs/101).
            settings['developerInstructions'] = (settings.get('developerInstructions') or '') + (
                '\n\nThis task is read-only: it runs beside the conversation\'s main work. Read, search, '
                'look up and calculate. Do not change files, apps or settings, and do not use desktop tools. '
                'If the request needs any of that, say that the main work must do it.')
        else:
            # Each writable task owns all its MCP workers; revoking the lease
            # cancels their process group without killing detached GUI apps.
            for name in names:
                if name != 'rungic-desktop':
                    config[f'mcp_servers.{name}.enabled'] = False
            config['mcp_servers.rungic-desktop.command'] = 'rungic-task-tools'
            config['mcp_servers.rungic-desktop.args'] = ['--task', task, '--', 'rungic-cua', 'mcp']
            env = dict(config.get('mcp_servers.rungic-desktop.env') or {})
            env['RUNGIC_TASK_ID'] = task
            config['mcp_servers.rungic-desktop.env'] = env
        return settings

    def _shared_rpc(self, method, params, task, read_only, conversation):
        """A task that may act, on the conversation's own thread (the executor's): thread/start is
        that thread, turn/start a turn there or, while one runs, joining it. -> the result, or None
        when the task is not for that thread (read-only, or no executor)."""
        if self.executor is None:
            return None
        if method == 'thread/start' and not read_only:
            thread = self.executor.main_thread(conversation)
            if not thread:
                return None
            if self.executor.running_turn(thread):
                # A new job while the work runs goes on at the same time (2026-10-05, the user:
                # "make it parallel"), in a workspace of its own so that two never act on one
                # screen. A request about the running work is steer_task's. None free: it joins.
                side = self._side_thread(task, conversation)
                if side is not None:
                    return side
            with self.lock:
                self.shared.setdefault(thread, set()).add(task)
            return {'thread': {'id': thread}, 'shared': True}
        thread = params.get('threadId')
        with self.lock:
            if method == 'thread/resume' and params.get('shared'):
                # The coordinator's journal after a restart (Reconcile): a task on the conversation's
                # own thread, which push-to-talk resumes and owns; never claimed as a thread of its own.
                self.shared.setdefault(thread, set()).add(task)
            shared = thread in self.shared
        if not shared:
            return None
        if method == 'thread/resume':
            return {'thread': {'id': thread}, 'shared': True}
        if method == 'turn/start':
            # A turn there, or into the one running (push-to-talk's, typing's, another task's), by the
            # one path every way of talking uses (task_control.py). The coordinator tracks this task
            # itself: nothing is kept for later when it cannot go now.
            done = self.executor.control.steer(thread, params['input'], source='call', durable=False)
            return {'turn': {'id': done.get('turn', '')}, 'joined': done.get('outcome') == 'steered'}
        return None

    def _steer(self, params):
        """steer_task's words for a running task (any thread): kept until they reach it (task_control.py).
        Its turn just ended: a new turn on its thread goes on with them -> {'turn': that turn, 'restarted'}.
        Codex not ready: {'queued': True}, they go as soon as it is."""
        done = self.executor.control.steer(params['threadId'], params['input'], source='call')
        if done.get('outcome') == 'queued':
            return {'queued': True}
        return {'turn': {'id': done.get('turn', '')}, 'restarted': done.get('outcome') == 'started'}

    def _side_thread(self, task, conversation):
        """A thread of its own for parallel acting work, in a free agent workspace -> the
        thread/start result, or None (no workspace free, or none offered)."""
        slot, params = self._side_params(task, conversation)
        if slot is None:
            return None
        result = self.server().call('thread/start', params)
        thread = (result.get('thread') or {}).get('id')
        if thread:
            self._adopt_side(thread, slot, task, conversation)
        else:
            self._free(slot)
        return result

    def _side_params(self, task, conversation, prefer=None):
        """A free agent workspace for parallel acting work and the thread settings that put it there
        -> (slot, settings), or (None, None)."""
        try:
            from rungic_cua import workspace
            exclude = {self.executor.main_workspace()}
            slot = None
            if prefer is not None and int(prefer) not in exclude:
                # Its own workspace again (a restart): the windows it worked with are there.
                slot = workspace.claim({'pid': os.getpid(), 'task': task},
                                       exclude=exclude | {s for s in workspace.slots() if s != int(prefer)})
            if slot is None:
                slot = workspace.claim({'pid': os.getpid(), 'task': task}, exclude=tuple(exclude))
        except Exception:  # noqa: BLE001 - no workspaces here: the work joins the running turn
            return None, None
        if slot is None:
            return None, None
        env = self.executor.workspace_settings(slot)
        if not env:
            self._free(slot)
            return None, None
        params = self._task_settings(task, False)
        config = params['config']
        config['shell_environment_policy.set'] = env
        config['mcp_servers.rungic-desktop.env'] = {**env, 'RUNGIC_TASK_ID': task}
        if conversation:
            params['developerInstructions'] = (params.get('developerInstructions') or '') + self.context(conversation) + (
                f'\n\nThis task runs at the same time as the conversation\'s main work, in workspace {slot}, '
                'an assistant\'s screen of its own. Work there; do not touch the main work\'s apps.')
        return slot, params

    def _adopt_side(self, thread, slot, task, conversation):
        with self.lock:
            self.threads[thread] = {'taskId': task, 'conversation': conversation}
            self.side_slots[thread] = self.side_home[thread] = slot
        # Who works in the workspace, for the director's controls (VoiceAgent.screen_work).
        try:
            from rungic_cua import workspace
            workspace.claim_path(slot).write_text(json.dumps({'pid': os.getpid(), 'task': task, 'thread': thread,
                                                              'since': round(time.time())}))
        except Exception:  # noqa: BLE001 - the director's controls are without it, the work is not
            pass

    def _free(self, slot):
        try:
            from rungic_cua import workspace
            workspace.release(slot, os.getpid())
        except Exception:  # noqa: BLE001 - a claim of a gone process frees itself
            pass

    def work_of(self, thread):
        """The call's work on `thread` (task_control.py): a parallel task in its workspace, a read-only
        task, or None (not this session's: the conversation's own thread is VoiceAgent's)."""
        with self.lock:
            info = self.threads.get(thread)
            if not info:
                return None
            slot = self.side_home.get(thread)
            return {'kind': 'side' if slot is not None else 'read', 'task': info.get('taskId', ''),
                    'conversation': info.get('conversation', ''), 'workspace': slot}

    def resume_work(self, record):
        """Load again, with its own settings, a thread whose work a restart cut (VoiceAgent.resume_work)
        -> True when it is ready for a turn."""
        thread, task, conversation = record['thread'], record.get('task', ''), record.get('conversation', '')
        if record.get('kind') == 'side':
            slot, params = self._side_params(task, conversation, prefer=record.get('workspace'))
            if slot is None:
                return False
            try:
                self.server().call('thread/resume', {**params, 'threadId': thread})
            except Exception:
                self._free(slot)
                raise
            self._adopt_side(thread, slot, task, conversation)
            return True
        params = self._task_settings(task, True)
        if conversation:
            params['developerInstructions'] = params.get('developerInstructions', '') + self.context(conversation)
        self.server().call('thread/resume', {**params, 'threadId': thread})
        with self.lock:
            self.threads[thread] = {'taskId': task, 'conversation': conversation}
        return True

    def _keep_claim(self, thread):
        """A parallel task at work keeps its workspace: a claim untouched for 20 minutes is free again
        (rungic_cua.workspace), and a long drawing outlived it."""
        with self.lock:
            slot = self.side_slots.get(thread)
            if slot is None or time.monotonic() - self.claim_touched.get(thread, 0) < 60:
                return
            self.claim_touched[thread] = time.monotonic()
        try:
            from rungic_cua import workspace
            workspace.touch_claim(slot)
        except Exception:  # noqa: BLE001 - kept for the next one
            pass

    def _reclaim_side(self, thread):
        with self.lock:
            slot = self.side_home.get(thread)
            info = self.threads.get(thread) or {}
            if slot is None or thread in self.side_slots:
                return
        try:
            from rungic_cua import workspace
            got = workspace.claim({'pid': os.getpid(), 'task': info.get('taskId', ''), 'thread': thread},
                                  exclude=[s for s in workspace.slots() if s != slot])
        except Exception:  # noqa: BLE001
            got = None
        if got is None:
            print(f'phone session: workspace {slot} of {thread} is taken; its work goes on there', flush=True)
            return
        with self.lock:
            self.side_slots[thread] = got

    def _release_side(self, thread):
        with self.lock:
            slot = self.side_slots.pop(thread, None)
        if slot is not None:
            self._free(slot)

    def _rpc(self, message):
        params = dict(message.get('params') or {})
        method, task = message['method'], params.pop('taskId', '')
        read_only = params.pop('readOnly', False)
        conversation = params.pop('conversation', '')
        try:
            shared = self._shared_rpc(method, params, task, read_only, conversation)
            if shared is None and method == 'turn/steer' and self.executor is not None:
                shared = self._steer(params)
            if shared is None and method == 'thread/resume':
                with self.lock:
                    known = params.get('threadId') in self.threads
                if known:
                    # Already loaded with its own settings (a parallel task's workspace, read-only):
                    # resuming with the default ones would take them away.
                    shared = {'thread': {'id': params['threadId']}}
            if shared is not None:
                self._write({'type': 'rpc-result', 'id': message['id'], 'result': shared})
                return
            params.pop('shared', None)
            if method in ('thread/start', 'thread/resume'):
                params = {**self._task_settings(task, read_only), **params}
                if conversation:
                    params['developerInstructions'] = params.get('developerInstructions', '') + self.context(conversation)
            if method == 'ServerResponse':
                self.server().respond(params['requestId'], params['result'])
                result = {}
            else:
                result = self.server().call(method, params)
            if method in ('thread/start', 'thread/resume'):
                thread = (result.get('thread') or {}).get('id') or params.get('threadId')
                if thread:
                    with self.lock:
                        self.threads[thread] = {'taskId': task, 'conversation': conversation}
            self._write({'type': 'rpc-result', 'id': message['id'], 'result': result})
        except Exception as error:
            self._write({'type': 'rpc-result', 'id': message['id'], 'error': str(error), 'uncertain': isinstance(error, (TimeoutError, OSError, EOFError))})

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self._handle(json.loads(line))
                except Exception as error:
                    # One bad line is dropped and said, never the end of the session.
                    print(f'phone session: dropped a message ({error!r}): {line[:300]!r}', flush=True)
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            with self.lock:
                self.snapshot.update(sessionId='', phase='closed')
                for event, result in self.pending.values():
                    result['error'] = 'Phone session service stopped'
                    event.set()
            self.emit({'type': 'phone-state', 'conversation': self.snapshot.get('conversation', ''),
                       'sessionId': '', 'phase': 'closed', 'microphone': False}, False)
            if hasattr(self, 'voice'):
                with self.lock:
                    self.voice.update(sessionId='', phase='closed')
                self.emit({'type': 'voice-state', 'sessionId': '', 'phase': 'closed', 'mode': 'press'}, False)

    def _handle(self, message):
        kind = message.get('type')
        if kind == 'reply':
            with self.lock:
                pair = self.pending.get(message.get('id'))
                if pair:
                    pair[1].update(message.get('result') or {})
                    pair[0].set()
        elif kind == 'rpc':
            threading.Thread(target=self._rpc, args=(message,), daemon=True).start()
        elif kind == 'event':
            event = message['event']
            if event.get('type') == 'voice-state':
                with self.lock:
                    self.voice.update(event)
            if event.get('type') == 'phone-state':
                with self.lock:
                    self.snapshot.update(event)
                self._mark_call(bool(self.snapshot.get('sessionId')))
            self.emit(event, message.get('keep', True))

    def _mark_call(self, active):
        """While a call is open the phone stays awake (rungic-agent-wakelock): the call goes on with
        the screen locked, as a phone call does (2026-10-05). Plasma hidden no longer ends it; it
        ends when the user hangs up. The marker names the session's process, so a crashed session
        holds nothing."""
        path = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}') / 'rungic-call.busy'
        try:
            if active:
                if not path.exists():
                    path.write_text(json.dumps({'pid': getattr(self.process, 'pid', 0)}))
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
