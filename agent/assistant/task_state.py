# SPDX-License-Identifier: MIT
"""What an agent turn is doing, in words people read (docs/89).

One TurnState per Codex turn, fed from the app-server's notifications:

  turn/plan/updated                    the plan Codex keeps (update_plan): steps and their status
  item/started, item/completed         commands (with Codex's own parse, commandActions), file
                                       changes, tool calls, web searches (reasoning is not a step)
  item/commandExecution/outputDelta    a running command's output: its last line and progress
  item/fileChange/patchUpdated         files being written, with lines added and removed
  the assistant's screen's caption     rungic_cua.activity (docs/88), for desktop work; with a live
                                       picture (a Blender render's latest pass, docs/90) the card
                                       shows it and refreshes it as passes come
  agentMessage (commentary)            Codex's own words about what it is about to do

snapshot() is what the chat shows (the task card); facts() is what the voice may say, in tenses
it cannot confuse: done, now, next. Nothing here talks to anyone; the voice agent decides when.
The card's words are in the desktop's language (voice_i18n); facts() is for the voice model, in
English around them (it speaks the user's language).
"""
from __future__ import annotations

import os
import re
import shlex
import time

from voice_i18n import _, ngettext

ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07')
PERCENT = re.compile(r'(?<![\d.])(\d{1,3}(?:\.\d+)?)\s?%')
COUNTED = re.compile(r'(?i)\b(sample|tile|frame|step|chunk|file|item|part|epoch|batch)s?\b\D{0,4}(\d+)\s*/\s*(\d+)')
SHELL = re.compile(r"^/bin/(?:ba)?sh -lc (['\"])([\s\S]*)\1$")

# Programs by what they do for the user. The first match of the command's program wins.
# (The words are looked up when shown: N_ only marks them for the catalog.)
def N_(message: str) -> str:
    return message


PROGRAMS = [
    ({'blender'}, N_('Run Blender')),
    ({'pkcon', 'apt', 'apt-get', 'dpkg', 'aptitude'}, N_('Install or look up packages')),
    ({'flatpak'}, N_('Install or manage Flatpak apps')),
    ({'pip', 'pip3', 'uv', 'pipx'}, N_('Install Python packages')),
    ({'npm', 'pnpm', 'yarn', 'npx', 'node'}, N_('Run Node.js')),
    ({'make', 'cmake', 'ninja', 'cargo', 'gcc', 'g++', 'clang', 'meson', 'go'}, N_('Build')),
    ({'git'}, 'Git'),
    ({'curl', 'wget', 'aria2c'}, N_('Access the network')),
    ({'ffmpeg', 'ffprobe'}, N_('Process audio and video')),
    ({'convert', 'magick', 'identify'}, N_('Process images')),
    ({'spectacle', 'rungic-screenshot'}, N_('Take a screenshot')),
    ({'rungic-cast'}, N_('Cast the screen')),
    ({'rungic-platform'}, N_('Use phone functions')),
    ({'rungic-agent-screen'}, N_("Turn the assistant's screen on or off")),
    ({'rungic-desktop-mode'}, N_('Turn desktop mode on or off')),
    ({'rungic-cua'}, N_('Operate the desktop')),
    ({'journalctl', 'dmesg', 'coredumpctl'}, N_('Read logs')),
    ({'systemctl'}, N_('Check or manage system services')),
    ({'df', 'du', 'free', 'upower', 'lscpu', 'uptime'}, N_('Check the device status')),
    ({'ls', 'find', 'fd', 'tree'}, N_('Find files')),
    ({'cat', 'head', 'tail', 'less', 'bat', 'nl', 'wc'}, N_('Read files')),
    ({'grep', 'rg', 'ag'}, N_('Search text')),
    ({'file', 'stat', 'sha256sum', 'md5sum'}, N_('Check files')),
    ({'rm', 'rmdir', 'trash', 'gio'}, N_('Delete or tidy up files')),
    ({'cp', 'mv', 'mkdir', 'ln', 'touch', 'tar', 'unzip', 'zip'}, N_('Tidy up files')),
    ({'sleep'}, N_('Wait')),
    ({'kill', 'pkill', 'killall'}, N_('End processes')),
]
PYTHON = {'python', 'python3'}
SHELL_WORDS = {'cd', 'export', 'set', 'source', '.', 'env', 'sudo', 'nice', 'timeout', 'time', 'nohup', 'exec'}


def unwrap(command: str) -> str:
    """The command without the shell wrapper Codex adds."""
    match = SHELL.match(command.strip())
    return match.group(2) if match else command.strip()


def first_program(command: str) -> tuple[str, list[str]]:
    """The first real program of a command line and its arguments (after cd, env, &&, pipes)."""
    for part in re.split(r'&&|\|\||;|\|', command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and (words[0] in SHELL_WORDS or '=' in words[0] or words[0].startswith('-')
                         or re.fullmatch(r'\d+[smh]?', words[0])):
            if words[0] == 'cd':
                words = []
                break
            words = words[1:]
        if words:
            return os.path.basename(words[0]), words[1:]
    return '', []


def describe_command(command: str, actions: list | None = None) -> str:
    """What a command does, in a few words of the desktop's language."""
    for action in actions or []:
        kind = action.get('type')
        if kind == 'read':
            return _('Read {name}').format(name=action.get('name') or os.path.basename(str(action.get('path') or '')))
        if kind == 'listFiles':
            path = action.get('path')
            if not path:
                return _('List files')
            return _('List the files in {folder}').format(folder=os.path.basename(str(path).rstrip('/')) or path)
        if kind == 'search':
            query = action.get('query')
            return _('Search for “{query}”').format(query=query) if query else _('Search files')
    program, args = first_program(unwrap(command))
    if program in PYTHON:
        script = next((a for a in args if a.endswith('.py')), '')
        if '-m' in args and args.index('-m') + 1 < len(args):
            return _('Run the Python module {module}').format(module=args[args.index('-m') + 1])
        return _('Run the Python script {script}').format(script=os.path.basename(script)) if script else _('Run Python')
    if program == 'blender':
        script = next((a for a in args if a.endswith('.py')), '')
        background = '-b' in args or '--background' in args
        if script:
            what = _('Run Blender in the background ({script})') if background else _('Run Blender ({script})')
            return what.format(script=os.path.basename(script))
        return _('Run Blender in the background') if background else _('Run Blender')
    if program == 'git' and args:
        return f'Git {args[0]}'
    for names, text in PROGRAMS:
        if program in names:
            return _(text)
    return _('Run {program}').format(program=program) if program else _('Run a command')


def last_line(text: str) -> str:
    """The last non-empty line of terminal output (carriage returns redraw a line)."""
    text = ANSI.sub('', text)
    for line in reversed(re.split(r'[\r\n]+', text)):
        line = ' '.join(line.split())
        if line:
            return line[:160]
    return ''


def progress_of(line: str) -> float | None:
    """A fraction done, when the line says so plainly (a percentage, or "Sample 12/64")."""
    counted = COUNTED.search(line)
    if counted and int(counted.group(3)) > 0:
        return min(1.0, int(counted.group(2)) / int(counted.group(3)))
    percent = PERCENT.search(line)
    if percent and float(percent.group(1)) <= 100:
        return float(percent.group(1)) / 100
    return None


def count_lines(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            added += 1
        elif line.startswith('-') and not line.startswith('---'):
            removed += 1
    return added, removed


def describe_files(changes: list) -> tuple[str, list[dict]]:
    files = []
    for change in changes or []:
        kind = (change.get('kind') or {}).get('type', 'update')
        added, removed = count_lines(change.get('diff') or '')
        files.append({'path': change.get('path', ''), 'kind': kind, 'added': added, 'removed': removed})
    if not files:
        return _('Change files'), files
    kind, name, count = files[0]['kind'], os.path.basename(files[0]['path']), len(files)
    if count == 1:
        text = {'add': _('Create {name}'), 'delete': _('Delete {name}')}.get(kind, _('Edit {name}'))
    elif kind == 'add':
        text = ngettext('Create {name} and others ({count} file)', 'Create {name} and others ({count} files)', count)
    elif kind == 'delete':
        text = ngettext('Delete {name} and others ({count} file)', 'Delete {name} and others ({count} files)', count)
    else:
        text = ngettext('Edit {name} and others ({count} file)', 'Edit {name} and others ({count} files)', count)
    return text.format(name=name, count=count), files


class TurnState:
    """One agent turn, as the task card and the voice see it."""

    RECENT = 12      # finished activities kept for the card

    def __init__(self, now: float | None = None) -> None:
        self.started = now if now is not None else time.time()
        self.plan: list[dict] = []           # [{'step', 'status': pending|inProgress|completed}]
        self.explanation = ''
        self.current: dict | None = None     # {'id', 'kind', 'text', 'detail', 'progress', 'since'}
        self.recent: list[dict] = []         # finished activities, newest last: {'kind', 'text', 'ok', 'seconds'}
        self.files: dict[str, dict] = {}     # path -> {'kind', 'added', 'removed'} over the turn
        self.intent = ''                     # Codex's latest commentary: an intention, not a result
        self.intent_at = 0.0
        self.output: dict[str, str] = {}     # item id -> recent output text (tail)
        self.preview: dict | None = None     # {'image', 'text', 'progress', 'done'}: a live picture

    # ---- inputs --------------------------------------------------------------------------
    def on_plan(self, plan: list, explanation: str | None = None) -> bool:
        """Codex's plan changed. True when the step in progress is a different one."""
        before = self.step_now()
        self.plan = [{'step': str(p.get('step', '')), 'status': str(p.get('status', 'pending'))} for p in plan or []]
        if explanation:
            self.explanation = explanation
        return self.step_now() != before

    def on_item(self, item: dict, completed: bool, now: float | None = None) -> bool:
        """An item started or finished. True when the card should change."""
        now = now if now is not None else time.time()
        kind = item.get('type')
        item_id = item.get('id', '')
        if kind == 'commandExecution':
            # Reading its own instructions (a skill, ~/.codex) is no step of the user's work: told,
            # it came out as "I read SKILL.md" (2026-10-05).
            if '/.codex/' in item.get('command', '') and not item.get('exitCode'):
                paths = [str(a.get('path') or '') for a in item.get('commandActions') or [] if a.get('type') == 'read']
                if not paths or all('/.codex/' in p for p in paths):
                    return False
            text, activity = describe_command(item.get('command', ''), item.get('commandActions')), 'command'
        elif kind == 'fileChange':
            text, files = describe_files(item.get('changes'))
            activity = 'files'
            if completed:
                for f in files:
                    total = self.files.setdefault(f['path'], {'kind': f['kind'], 'added': 0, 'removed': 0})
                    total['added'] += f['added']
                    total['removed'] += f['removed']
        elif kind == 'mcpToolCall':
            tool = item.get('tool', '')
            if tool.startswith('desktop_'):
                text = _("Work on the assistant's screen")
            elif tool == 'team_post':
                # A team's post (docs/research/91 §14): what it said, the board has the rest.
                said = (item.get('arguments') or {}).get('text') or ''
                text = _('Tell the team: {text}').format(text=said) if said else _('Tell the team')
            else:
                text = _('Use the tool {tool}').format(tool=tool)
            activity = 'screen' if tool.startswith('desktop_') else 'tool'
        elif kind == 'webSearch':
            query = item.get('query') or ''
            text = _('Search the web for “{query}”').format(query=query) if query else _('Search the web')
            activity = 'search'
        elif kind == 'imageView':
            picture = os.path.basename(item.get('path') or '')
            text = _('Look at the picture {name}').format(name=picture) if picture else _('Look at a picture')
            activity = 'look'
        elif kind == 'imageGeneration':
            text, activity = _('Generate a picture'), 'image'
        elif kind == 'sleep':
            text, activity = _('Wait'), 'wait'
        else:
            return False
        # Reasoning is not a step: between steps the card says it is thinking by itself.
        if not completed:
            self.current = {'id': item_id, 'kind': activity, 'text': text, 'detail': '', 'progress': None, 'since': now}
            return True
        seconds = round(now - self.current['since']) if self.current and self.current.get('id') == item_id else 0
        ok = not (kind == 'commandExecution' and item.get('exitCode') not in (None, 0))
        self.recent.append({'kind': activity, 'text': text, 'ok': ok, 'seconds': seconds})
        self.recent = self.recent[-self.RECENT:]
        if self.current and self.current.get('id') == item_id:
            self.current = None
        self.output.pop(item_id, None)
        return True

    def on_output(self, item_id: str, delta: str) -> bool:
        """Output of the running command. True when what the card shows changed."""
        tail = (self.output.get(item_id, '') + delta)[-4000:]
        self.output[item_id] = tail
        if not self.current or self.current.get('id') != item_id:
            return False
        line = last_line(tail)
        progress = progress_of(line)
        if progress is None and self.current.get('progress') is not None:
            progress = self.current['progress']       # keep the last known fraction
        changed = line != self.current.get('detail') or progress != self.current.get('progress')
        self.current['detail'] = line
        self.current['progress'] = progress
        return changed

    def on_patch(self, item_id: str, changes: list) -> bool:
        text, files = describe_files(changes)
        if self.current and self.current.get('id') == item_id:
            added = sum(f['added'] for f in files)
            removed = sum(f['removed'] for f in files)
            self.current['text'] = text
            lines = ngettext('+{added} −{removed} line', '+{added} −{removed} lines', added + removed)
            self.current['detail'] = lines.format(added=added, removed=removed) if added or removed else ''
            return True
        return False

    def on_screen(self, caption: str) -> bool:
        """The assistant's screen's caption (rungic_cua.activity) while a desktop tool runs."""
        if not self.current or self.current.get('kind') != 'screen' or not caption:
            return False
        if self.current.get('detail') == caption:
            return False
        self.current['detail'] = caption
        return True

    def on_live(self, report: dict) -> bool:
        """A report with a picture on the activity channel (rungic_cua.activity), written during
        this turn: the card shows the latest one."""
        if not report.get('image') or float(report.get('time') or 0) < self.started:
            return False
        preview = {'image': report['image'], 'text': report.get('text', ''), 'progress': report.get('progress'),
                   'done': report.get('state') == 'done'}
        if preview == self.preview:
            return False
        self.preview = preview
        return True

    def on_commentary(self, text: str, now: float | None = None) -> None:
        self.intent = ' '.join(text.split())[:200]
        self.intent_at = now if now is not None else time.time()

    # ---- outputs -------------------------------------------------------------------------
    def step_now(self) -> str:
        return next((p['step'] for p in self.plan if p['status'] == 'inProgress'), '')

    def snapshot(self, now: float | None = None) -> dict:
        """The task card: plan, what is happening now, what was done, files touched."""
        now = now if now is not None else time.time()
        current = dict(self.current) if self.current else None
        if current:
            current['seconds'] = round(now - current.pop('since'))
            current.pop('id', None)
        return {'plan': self.plan, 'explanation': self.explanation, 'current': current,
                'recent': self.recent[-6:], 'intent': self.intent, 'preview': self.preview,
                'files': [{'path': p, **f} for p, f in self.files.items()]}

    def facts(self, now: float | None = None) -> str:
        """What the voice may say, by tense: done, now, next. Only facts; an intention says so.
        For the voice model: English labels around the card's words (prompts/realtime.md)."""
        now = now if now is not None else time.time()
        lines = [f'Time so far: {round(now - self.started)} s; the task is still running.']
        if self.plan:
            done = [p['step'] for p in self.plan if p['status'] == 'completed']
            doing = [p['step'] for p in self.plan if p['status'] == 'inProgress']
            todo = [p['step'] for p in self.plan if p['status'] == 'pending']
            lines.append(f'The plan has {len(self.plan)} steps.')
            if done:
                lines.append('Done: ' + '; '.join(done))
            if doing:
                lines.append('In progress: ' + '; '.join(doing))
            if todo:
                lines.append('Not started yet: ' + '; '.join(todo[:3]))
        if self.current:
            detail = self.current.get('detail') or ''
            progress = self.current.get('progress')
            extra = [f"{round(now - self.current['since'])} s"]
            if progress is not None:
                extra.append(f'about {round(progress * 100)}%')
            if detail and self.current.get('kind') == 'screen':
                extra.append(detail)
            lines.append(f"Now: {self.current['text']} ({', '.join(extra)})")
        elif self.recent:
            lines.append(f"Just finished: {self.recent[-1]['text']}")
        if self.preview and not self.preview['done'] and self.preview.get('progress') is not None:
            lines.append(f"Render progress: {self.preview['text']} (about {round(self.preview['progress'] * 100)}%, "
                         'the picture is getting clearer pass by pass)')
        if self.intent and now - self.intent_at < 60:
            lines.append(f'Intends next: {self.intent} (an intention, not done yet)')
        return '\n'.join(lines)
