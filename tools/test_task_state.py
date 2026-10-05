"""The task state of an agent turn (agent/assistant/task_state.py, docs/89)."""
import ast
import gettext
import os
import sys
from pathlib import Path

# The card's words are English in the source; a Chinese desktop reads them from the catalog
# (po/zh_CN/rungic-voice-agent.po), checked at the end.
os.environ['LANGUAGE'] = 'en_US'
VOICE_AGENT = Path(__file__).resolve().parent.parent / 'agent/assistant'
sys.path.insert(0, str(VOICE_AGENT))
import task_state as ts  # noqa: E402


# covers: agent.progress/E2
def test_commands_read_as_what_they_do():
    assert ts.describe_command("/bin/bash -lc 'blender -b -t 6 --python /home/u/make_donut.py'") == 'Run Blender in the background (make_donut.py)'
    assert ts.describe_command("/bin/bash -lc 'cd /tmp && python3 render.py --fast'") == 'Run the Python script render.py'
    assert ts.describe_command("/bin/bash -lc 'timeout 60 pkcon install-local -y a.deb'") == 'Install or look up packages'
    assert ts.describe_command('/bin/bash -lc "git status --short"') == 'Git status'
    assert ts.describe_command('/bin/bash -lc "sed -n 1,80p x.py"',
                               [{'type': 'read', 'name': 'x.py', 'path': '/h/x.py', 'command': 'sed'}]) == 'Read x.py'
    assert ts.describe_command('whatever-tool --x') == 'Run whatever-tool'


# covers: agent.progress/E2
def test_output_gives_the_last_line_and_progress():
    assert ts.last_line('a\nFra:1 Mem:12M | Sample 12/64\r\x1b[2K') == 'Fra:1 Mem:12M | Sample 12/64'
    assert ts.progress_of('Fra:1 Mem:12M | Sample 16/64') == 0.25
    assert ts.progress_of('Downloading  45%|####') == 0.45
    assert ts.progress_of('2026-09-29 12/10 notes') is None


# covers: agent.progress/E1 agent.progress/E2 agent.progress/E4
def test_a_turn_with_a_plan():
    state = ts.TurnState(now=100)
    assert state.on_plan([{'step': '写建模脚本', 'status': 'inProgress'}, {'step': '渲染', 'status': 'pending'}])
    assert not state.on_plan([{'step': '写建模脚本', 'status': 'inProgress'}, {'step': '渲染', 'status': 'pending'}])
    state.on_item({'type': 'fileChange', 'id': 'f1', 'changes': [
        {'path': '/h/make.py', 'kind': {'type': 'add'}, 'diff': '+a\n+b\n'}]}, False, now=101)
    assert state.snapshot(now=102)['current']['text'] == 'Create make.py'
    state.on_item({'type': 'fileChange', 'id': 'f1', 'status': 'completed', 'changes': [
        {'path': '/h/make.py', 'kind': {'type': 'add'}, 'diff': '+a\n+b\n'}]}, True, now=103)
    assert state.on_plan([{'step': '写建模脚本', 'status': 'completed'}, {'step': '渲染', 'status': 'inProgress'}])
    state.on_item({'type': 'commandExecution', 'id': 'c1', 'command': "/bin/bash -lc 'blender -b --python /h/make.py'"},
                  False, now=104)
    assert state.on_output('c1', 'Saved\nFra:1 | Sample 32/64\n')
    snap = state.snapshot(now=130)
    assert snap['current'] == {'kind': 'command', 'text': 'Run Blender in the background (make.py)', 'detail': 'Fra:1 | Sample 32/64',
                               'progress': 0.5, 'seconds': 26}
    assert snap['files'] == [{'path': '/h/make.py', 'kind': 'add', 'added': 2, 'removed': 0}]
    facts = state.facts(now=130)
    assert 'Done: 写建模脚本' in facts and 'In progress: 渲染' in facts and 'about 50%' in facts
    state.on_item({'type': 'commandExecution', 'id': 'c1', 'exitCode': 1, 'command': 'blender'}, True, now=140)
    assert state.snapshot(now=141)['recent'][-1] == {'kind': 'command', 'text': 'Run Blender', 'ok': False, 'seconds': 36}


# covers: agent.progress/E4
def test_commentary_is_an_intention():
    state = ts.TurnState(now=0)
    state.on_commentary('我会做一个甜甜圈，完成后附在回复里。', now=5)
    assert 'an intention, not done yet' in state.facts(now=10)
    assert 'intention' not in state.facts(now=100)


# covers: agent.progress/E5
def test_screen_captions_follow_the_desktop_tool():
    state = ts.TurnState(now=0)
    assert not state.on_screen('打开“渲染”菜单')          # no desktop tool running
    state.on_item({'type': 'mcpToolCall', 'id': 'm1', 'tool': 'desktop_goal'}, False, now=1)
    assert state.on_screen('打开“渲染”菜单')
    assert '打开“渲染”菜单' in state.facts(now=3)


# covers: agent.progress/E6
def test_a_live_picture_is_taken_from_this_turn_only():
    state = ts.TurnState(now=100)
    assert not state.on_live({'image': '/run/x-0.jpg', 'text': 'Blender 渲染 · 4/64 采样', 'progress': 0.06, 'time': 99})
    assert state.on_live({'image': '/run/x-1.jpg', 'text': 'Blender 渲染 · 12/64 采样', 'progress': 0.19, 'time': 101,
                          'state': 'working'})
    assert not state.on_live({'image': '/run/x-1.jpg', 'text': 'Blender 渲染 · 12/64 采样', 'progress': 0.19, 'time': 102,
                              'state': 'working'})
    assert state.snapshot(now=103)['preview'] == {'image': '/run/x-1.jpg', 'text': 'Blender 渲染 · 12/64 采样',
                                                  'progress': 0.19, 'done': False}
    assert 'about 19%' in state.facts(now=103)
    assert not state.on_live({'text': '点击', 'time': 104})


class PoCatalog(gettext.NullTranslations):
    """A .po read directly (msgfmt is not needed to test it): msgid -> msgstr, plural forms by n."""

    def __init__(self, path):
        super().__init__()
        self.messages, entry, field = {}, {}, None
        for line in path.read_text(encoding='utf-8').splitlines() + ['']:
            if line.startswith('"'):
                entry[field] += ast.literal_eval(line)
            elif line and not line.startswith('#'):
                field, value = line.split(' ', 1)
                entry[field] = ast.literal_eval(value)
            elif not line and entry:
                if entry.get('msgid'):
                    forms = [entry[k] for k in sorted(k for k in entry if k.startswith('msgstr['))]
                    self.messages[entry['msgid']] = forms or entry['msgstr']
                entry, field = {}, None

    def gettext(self, message):
        return self.messages.get(message) or message

    def ngettext(self, singular, plural, n):
        forms = self.messages.get(singular)
        return forms[0] if forms else (singular if n == 1 else plural)   # zh_CN: nplurals=1


# covers: agent.progress/E7
def test_a_chinese_desktop_reads_as_before(monkeypatch):
    zh = PoCatalog(VOICE_AGENT / 'po/zh_CN/rungic-voice-agent.po')
    monkeypatch.setattr(ts, '_', zh.gettext)
    monkeypatch.setattr(ts, 'ngettext', zh.ngettext)
    assert ts.describe_command("/bin/bash -lc 'blender -b -t 6 --python /home/u/make_donut.py'") == '在后台运行 Blender（make_donut.py）'
    assert ts.describe_command("/bin/bash -lc 'timeout 60 pkcon install-local -y a.deb'") == '安装或查询软件包'
    assert ts.describe_command('whatever-tool --x') == '运行 whatever-tool'
    changes = [{'path': f'/h/{n}.py', 'kind': {'type': 'add'}, 'diff': '+a\n'} for n in 'abc']
    assert ts.describe_files(changes)[0] == '新建 a.py 等 3 个文件'
    state = ts.TurnState(now=0)
    state.on_item({'type': 'fileChange', 'id': 'f1', 'changes': []}, False, now=1)
    state.on_patch('f1', [{'path': '/h/x.py', 'kind': {'type': 'update'}, 'diff': '+a\n+b\n-c\n'}])
    assert state.snapshot(now=2)['current']['text'] == '修改 x.py'
    assert state.snapshot(now=2)['current']['detail'] == '+2 −1 行'


# covers: agent.progress/E2
def test_reading_its_own_instructions_is_no_step():
    # 2026-10-05: reading a skill came out to the user as "I read SKILL.md".
    t = ts.TurnState(now=0)
    read = {'type': 'commandExecution', 'id': 'c1', 'command': "/bin/bash -lc 'cat /home/u/.codex/skills/rungic-phone-desktop/SKILL.md'",
            'commandActions': [{'type': 'read', 'name': 'SKILL.md', 'path': '/home/u/.codex/skills/rungic-phone-desktop/SKILL.md'}]}
    assert t.on_item(read, False, now=1) is False and t.on_item({**read, 'exitCode': 0}, True, now=2) is False
    assert t.current is None and t.recent == []
    work = {'type': 'commandExecution', 'id': 'c2', 'command': "/bin/bash -lc 'cat ~/Pictures/notes.txt'",
            'commandActions': [{'type': 'read', 'name': 'notes.txt', 'path': '/home/u/Pictures/notes.txt'}]}
    assert t.on_item(work, False, now=3) is True, "the user's own files are their work"
