# SPDX-License-Identifier: MIT
"""The generated sections of the assistant's prompts (tools/agent_capabilities.py, docs/59): the
capabilities (capabilities.yaml) in agent.md, and voice-common.md with them in both voice prompts
(phone.md and realtime.md).
The real prompts must be current, and every skill a capability names must ship with the package."""
from pathlib import Path
import re

import pytest

import agent_capabilities as ac

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / 'agent/assistant/skills'

SOURCE = '''
capabilities:
  - name: SMS
    can: Send SMS.
    when: '"发短信"'
    how: skill `rungic-messages-calls`
    external: Sending
  - name: Files
    can: Find files
    when: a file to find
    how: the shell
'''


def prompts(tmp_path):
    (tmp_path / 'capabilities.yaml').write_text(SOURCE)
    (tmp_path / 'voice-common.md').write_text('<!-- a note -->\n\n## Shared\n\n{{capabilities}}\n* After.\n')
    (tmp_path / 'agent.md').write_text(f'Before.\n{ac.BEGIN}\nold\n{ac.END}\nAfter.\n')
    for name in ac.VOICES:
        (tmp_path / name).write_text(f'{name} own.\n{ac.VOICE_BEGIN}\nold\n{ac.VOICE_END}\n{name} tail.\n')
    return tmp_path


# covers: agent.instructions/E5
def test_the_real_prompts_are_current():
    assert ac.stale() == {}, 'run: python3 tools/agent_capabilities.py render --write'


# covers: agent.instructions/E5
def test_a_stale_section_is_found_and_rendered_in_each_voice(tmp_path):
    base = prompts(tmp_path)
    changed = ac.stale(base, base / 'capabilities.yaml', base / 'voice-common.md')
    assert set(changed) == {'agent.md', *ac.VOICES}
    agent = changed['agent.md']
    assert agent.startswith('Before.\n') and agent.endswith(f'{ac.END}\nAfter.\n')
    assert ('- **SMS**: Send SMS. Use when: "发短信". How: skill `rungic-messages-calls`. '
            'Sending has an external effect') in agent
    assert '- **Files**: Find files. Use when: a file to find. How: the shell.\n' in agent
    # Both voices: the same shared section (2026-10-05: a call is only another way to talk), each
    # with its own text around it, and the note of voice-common.md left out.
    shared = [changed[name].split(ac.VOICE_BEGIN)[1].split(ac.VOICE_END)[0] for name in ac.VOICES]
    assert shared[0] == shared[1] and '## Shared' in shared[0] and 'a note' not in shared[0]
    assert '* SMS: Send SMS. When: "发短信". Sending needs the user\'s go-ahead.\n' in shared[0]
    assert changed['phone.md'].startswith('phone.md own.\n') and changed['phone.md'].endswith('phone.md tail.\n')
    for name, text in changed.items():
        (base / name).write_text(text)
    assert ac.stale(base, base / 'capabilities.yaml', base / 'voice-common.md') == {}


def test_a_prompt_without_the_markers_is_an_error(tmp_path):
    base = prompts(tmp_path)
    (base / 'phone.md').write_text('No section here.\n')
    with pytest.raises(ValueError, match='phone.md'):
        ac.stale(base, base / 'capabilities.yaml', base / 'voice-common.md')


def test_every_named_skill_ships():
    named = {m for entry in ac.load() for m in re.findall(r'skill `([a-z0-9-]+)`', entry['how'])}
    assert named
    for name in sorted(named):
        skill = SKILLS / name / 'SKILL.md'
        assert skill.exists(), f'capabilities.yaml names skill {name}, which does not exist'
        assert f'\nname: {name}\n' in skill.read_text()


# covers: agent.instructions/E5
def test_both_voices_speak_as_the_assistant_that_operates_the_phone():
    # 2026-10-05: in a call the voice said "I am only a voice assistant, I cannot use Krita" while
    # the executor was drawing in it. Push-to-talk's voice already had the rule; the call's lacked it.
    prompts = Path(__file__).resolve().parents[2] / 'agent/assistant/prompts'
    phone = (prompts / 'phone.md').read_text()
    realtime = (prompts / 'realtime.md').read_text()
    assert 'Never say that you cannot use' in phone and 'only a voice assistant' in phone
    assert 'Do not claim that you cannot perform some actions' in realtime


# covers: agent.instructions/E5
def test_found_in_the_acceptance_call_of_2026_10_05():
    # Asked to draw in Krita, the task generated an image and placed it there; asked to cast, the
    # voice told the user to choose the TV's input; progress named SKILL.md.
    prompts = Path(__file__).resolve().parents[2] / 'agent/assistant/prompts'
    agent = (prompts / 'agent.md').read_text()
    assert 'make it in that app with the app\'s own tools' in agent
    assert 'Do not name files you read, skills, tools or commands.' in agent
    for voice in ('phone.md', 'realtime.md'):
        text = (prompts / voice).read_text()
        assert 'Do not tell the user steps to take for it' in text, voice
        assert 'Do not mention files that execution read, skills, tools or commands.' in text, voice



# covers: agent.phone-mode/E7
def test_the_user_says_whether_after_or_beside():
    # 2026-10-05, the user: sequential or parallel is a capability, chosen by what is asked.
    phone = (Path(__file__).resolve().parents[2] / 'agent/assistant/prompts/phone.md').read_text()
    assert '"画完再…": steer_task' in phone and '"再开一个任务":\nstart_task' in phone
