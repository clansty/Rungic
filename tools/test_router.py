"""rungic_cua.router (docs/research/91): where the agent's desktop tools act.

The platform bridge and the per-session children are stand-ins: nothing starts."""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'agent/computer-use'))
from rungic_cua import router  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def workspace_up(tmp_path):
    """The workspace counts as running (workspace.ensure would start it); no murmur thread follows
    a session log (team.Murmur); the board goes to a temporary file, nothing to the app."""
    with mock.patch.object(router.workspace, 'ensure', return_value=True), \
            mock.patch.object(router.team, 'Murmur', mock.MagicMock()), \
            mock.patch.object(router.team, 'board_path', lambda: tmp_path / 'team-board.json'), \
            mock.patch.object(router.team, '_send', lambda request: None), \
            mock.patch.object(router.hold, 'DIR', tmp_path):
        yield


class FakeChild:
    made = []

    def __init__(self, env):
        self.env = env
        self.calls = []
        FakeChild.made.append(self)

    def alive(self):
        return True

    def request(self, method, params):
        self.calls.append(params['name'])
        return {'content': [{'type': 'text', 'text': '{}'}]}

    def close(self):
        pass


WORKSPACE_ENV = {'WAYLAND_DISPLAY': 'wayland-ws-1', 'RUNGIC_WORKSPACE': '1', 'DISPLAY': ':0',
                 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/tmp/ws-bus', 'XDG_RUNTIME_DIR': '/run/user/1000',
                 'RUNGIC_USER_WAYLAND_DISPLAY': 'wayland-0',
                 'RUNGIC_USER_DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'}


def in_workspace(env, slot, run=None):
    """router.workspace_env without rungic-workspace-env: the workspace's display and bus."""
    return {**env, 'WAYLAND_DISPLAY': f'wayland-ws-{slot}', 'RUNGIC_WORKSPACE': str(slot),
            'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/tmp/ws-{slot}-bus'}


def routed(desktop_state, name='desktop_launch', setting=None, desktop_on=False):
    """`desktop_on`: the user's desktop (workspace 0, desktop mode) runs; else `rungic-desktop-mode
    on` (a stand-in) turns it on."""
    FakeChild.made.clear()
    running = {'on': desktop_on}
    started = []

    def run(argv, **kwargs):
        started.append(argv)
        running['on'] = True
        return mock.Mock(returncode=0, stdout='', stderr='')
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value=desktop_state), \
            mock.patch.object(router, 'desktop_running', lambda: running['on']), \
            mock.patch.object(router, 'workspace_env', in_workspace), \
            mock.patch.object(router.subprocess, 'run', run):
        r = router.Router(WORKSPACE_ENV)
        told = r.call('desktop_where', {'target': setting}) if setting else None
        result = r.call(name, {'app': 'Kalk'})
    # desktop_where already said where; the next call adds nothing then.
    note = json.loads((told or result)['content'][-1]['text'])
    note['started'] = started
    return FakeChild.made[-1], note


# covers: agent.where/E1
def test_desktop_mode_on_works_on_the_users_desktop():
    """The user's desktop is workspace 0 (docs/research/97 §19): the child works in it."""
    child, note = routed({'enabled': False, 'tv': False}, desktop_on=True)
    assert note['where'] == 'desktop' and 'desktop mode is on' in note['why']
    assert note['workspace'] == 0 and note['shell'] == 'rungic-workspace-env 0 COMMAND'
    assert child.env['WAYLAND_DISPLAY'] == 'wayland-ws-0' and child.env['RUNGIC_WORKSPACE'] == '0'
    assert note['started'] == []


# covers: agent.where/E1
def test_a_tv_showing_the_desktop_works_there():
    child, note = routed({'enabled': False, 'tv': True})
    assert note['where'] == 'desktop' and 'TV' in note['why']
    assert note['started'] == [['rungic-desktop-mode', 'on']] and child.env['RUNGIC_WORKSPACE'] == '0'


# covers: agent.where/E1
def test_otherwise_its_own_workspace():
    child, note = routed({'enabled': False, 'tv': False})
    assert note['where'] == 'workspace'
    assert child.env['WAYLAND_DISPLAY'] == 'wayland-ws-1' and child.env['RUNGIC_WORKSPACE'] == '1'


# covers: agent.where/E2
def test_the_users_word_wins():
    child, note = routed({'enabled': False, 'tv': False}, setting='workspace', desktop_on=True)
    assert note['where'] == 'workspace' and note['why'] == 'the user said so'
    assert child.env['WAYLAND_DISPLAY'] == 'wayland-ws-1'
    # "On my desktop" with desktop mode off: turned on, and worked in.
    child, note = routed({'enabled': False, 'tv': False}, setting='desktop')
    assert note['where'] == 'desktop' and child.env['WAYLAND_DISPLAY'] == 'wayland-ws-0'
    assert note['started'] == [['rungic-desktop-mode', 'on']]


# covers: agent.where/E4
def test_no_bridge_means_the_workspace():
    FakeChild.made.clear()
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', side_effect=OSError('no socket')):
        r = router.Router(WORKSPACE_ENV)
        assert r.where()[0] == 'workspace'


# covers: agent.where/E3
def test_the_where_note_comes_only_when_it_changes():
    FakeChild.made.clear()
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value={'enabled': False, 'tv': False}):
        r = router.Router(WORKSPACE_ENV)
        first = r.call('desktop_screenshot', {})
        second = r.call('desktop_screenshot', {})
    assert len(first['content']) == 2 and len(second['content']) == 1



# covers: agent.workspaces/E5
def test_the_users_session_gets_its_own_values_back():
    """The workspace sets these as Plasma's desktop session has them (docs/103); the user's session
    gets its own values back, and loses one it never had."""
    workspace = {'WAYLAND_DISPLAY': 'wayland-ws-1', 'RUNGIC_WORKSPACE': '1', 'PLASMA_INTEGRATION_USE_PORTAL': '0',
                 'QT_QPA_PLATFORMTHEME': ''}
    env = router.user_session_env({**workspace, 'RUNGIC_USER_PLASMA_INTEGRATION_USE_PORTAL': '1',
                                   'RUNGIC_USER_QT_QPA_PLATFORMTHEME': 'KDE'})
    assert (env['PLASMA_INTEGRATION_USE_PORTAL'], env['QT_QPA_PLATFORMTHEME']) == ('1', 'KDE')
    assert not any(k.startswith('RUNGIC_USER_') for k in env)
    env = router.user_session_env(workspace)
    assert 'PLASMA_INTEGRATION_USE_PORTAL' not in env and 'QT_QPA_PLATFORMTHEME' not in env


# ---- a sub-agent's own workspace (docs/research/91, "由 Codex 当组长") -----------------------------
SUBAGENT_META = {'threadId': 'child-a', 'x-codex-turn-metadata': {
    'thread_source': 'subagent', 'thread_id': 'child-a', 'parent_thread_id': 'parent'}}
PARENT_META = {'threadId': 'parent', 'x-codex-turn-metadata': {'thread_source': 'user', 'thread_id': 'parent'}}


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Four host workspaces, claims in a temporary runtime directory, none running."""
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setattr(router.workspace, 'slots', lambda: [1, 2, 3, 4])
    monkeypatch.setattr(router.workspace, 'running', lambda slot, run=None: False)
    monkeypatch.setattr(router, 'workspace_env',
                        lambda env, slot: {**env, 'RUNGIC_WORKSPACE': str(slot), 'WAYLAND_DISPLAY': f'wayland-ws-{slot}'})
    return tmp_path


def sub_router(meta=SUBAGENT_META):
    r = router.Router(WORKSPACE_ENV)
    return r, json.loads(r.call('desktop_where', {}, meta)['content'][-1]['text'])


# covers: agent.team/E1
def test_a_subagent_takes_a_workspace_of_its_own(host):
    FakeChild.made.clear()
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value={'enabled': True, 'tv': False}):
        r, note = sub_router()
        # Not the parent's (1), and not the user's desktop although desktop mode is on.
        assert note['where'] == 'workspace' and note['workspace'] == 2
        assert note['shell'] == 'rungic-workspace-env 2 COMMAND' and 'yours' in note
        r.call('desktop_launch', {'app': 'Krita'}, SUBAGENT_META)
    assert FakeChild.made[-1].env['RUNGIC_WORKSPACE'] == '2'
    record = json.loads((host / 'rungic-workspace-2.busy').read_text())
    assert record['thread'] == 'child-a' and record['parent'] == 'parent' and record['pid'] > 0


# covers: agent.team/E1
def test_subagents_get_different_workspaces_and_the_parent_keeps_its_own(host):
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value={'enabled': False, 'tv': False}):
        _, a = sub_router()
        # Another process's claim: workspace 2 is held while that process lives.
        (host / 'rungic-workspace-2.busy').write_text(json.dumps({'pid': 1}))
        _, b = sub_router()
        _, parent = sub_router(PARENT_META)
    assert (a['workspace'], b['workspace'], parent['workspace']) == (2, 3, 1)
    assert 'yours' not in parent


# covers: agent.team/E2
def test_a_claim_of_an_ended_process_is_free_again(host):
    (host / 'rungic-workspace-2.busy').write_text(json.dumps({'pid': 999999999}))
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value={'enabled': False, 'tv': False}):
        _, note = sub_router()
    assert note['workspace'] == 2


# covers: agent.team/E2
def test_closing_gives_the_workspace_back(host):
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value={'enabled': False, 'tv': False}), \
            mock.patch.object(router.workspace, 'close', return_value={'closed': True}) as close:
        r, note = sub_router()
        r.call('desktop_close_workspace', {}, SUBAGENT_META)
    close.assert_called_once_with(2, force=False)
    assert not (host / 'rungic-workspace-2.busy').exists()


# covers: agent.team/E1
def test_all_taken(host):
    for slot in (2, 3, 4):
        (host / f'rungic-workspace-{slot}.busy').write_text('')     # runners' claims (tools/team)
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value={'enabled': False, 'tv': False}):
        r = router.Router(WORKSPACE_ENV)
        with pytest.raises(RuntimeError, match='every agent workspace is taken'):
            r.call('desktop_launch', {'app': 'Krita'}, SUBAGENT_META)


# ---- the team journal (team.py, docs/research/91 "实时看到团队讨论") --------------------------------
# covers: agent.team/E5
def test_a_members_post_goes_to_the_journal_and_its_tile(host, tmp_path, monkeypatch):
    told = []
    monkeypatch.setattr(router.team, 'tell_app', lambda slot, entry: told.append((slot, entry['kind'])))
    project = tmp_path / 'game'
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value={'enabled': False, 'tv': False}):
        r = router.Router(WORKSPACE_ENV)
        r.call('team_post', {'role': 'art', 'kind': 'review', 'text': 'Style is missing: pixel art?',
                             'project': str(project)}, SUBAGENT_META)
        r.call('team_post', {'role': 'art', 'kind': 'progress', 'text': 'Bird drawn'}, SUBAGENT_META)
    lines = [json.loads(line) for line in (project / '.team/journal.jsonl').read_text().splitlines()]
    assert [(e['role'], e['kind'], e['workspace']) for e in lines] == [('art', 'review', 2), ('art', 'progress', 2)]
    assert lines[0]['parent'] == 'parent'
    tile = json.loads((host / 'rungic-agent-screen/team-ws2.json').read_text())
    assert tile['text'] == 'Bird drawn' and told == [(2, 'review'), (2, 'progress')]


# covers: agent.team/E5
def test_a_member_ending_silent_gets_ended(host, tmp_path, monkeypatch):
    monkeypatch.setattr(router.team, 'tell_app', lambda slot, entry: None)
    project = tmp_path / 'game'
    with mock.patch.object(router, 'Child', FakeChild), \
            mock.patch.object(router, 'bridge', return_value={'enabled': False, 'tv': False}):
        r = router.Router(WORKSPACE_ENV)
        r.call('team_post', {'role': 'sound', 'kind': 'progress', 'text': 'Seeds made', 'project': str(project)},
               SUBAGENT_META)
        r.close()
    kinds = [json.loads(line)['kind'] for line in (project / '.team/journal.jsonl').read_text().splitlines()]
    assert kinds == ['progress', 'ended']


# covers: agent.team/E5
def test_the_leads_post_goes_on_its_own_workspace(host, tmp_path, monkeypatch):
    told = []
    monkeypatch.setattr(router.team, 'tell_app', lambda slot, entry: told.append(slot))
    project = tmp_path / 'game'
    with mock.patch.object(router, 'Child', FakeChild):
        r = router.Router(WORKSPACE_ENV)
        r.call('team_post', {'role': 'lead', 'kind': 'decision', 'text': 'Pixel art, 3 frames', 'project': str(project)},
               PARENT_META)
    entry = json.loads((project / '.team/journal.jsonl').read_text())
    assert entry['kind'] == 'decision' and entry['workspace'] == 1 and told == [1]


# ---- the murmur: tool calls as a few words (team.describe_call) ------------------------------------
# covers: agent.team/E4
def test_shell_commands_as_words():
    d = router.team.describe_command
    assert d("cat > /home/u/Projects/card/art/draw_card.py <<'PY'\nprint(1)\nPY") == 'Write draw_card.py'
    assert d('cat /home/u/Projects/card/BRIEF.md') == 'Read BRIEF.md'
    assert d('python3 /home/u/Projects/card/art/draw_card.py --check') == 'Run draw_card.py'
    assert d('rungic-workspace-env 2 krita --help') == 'Run krita'
    assert d("python3 - <<'PY'\nx\nPY") == 'Run a script'
    assert d('LC_ALL=C ffprobe -v error chime.wav') == 'Process the audio with ffprobe'


# covers: agent.team/E4
def test_code_mode_calls_as_words():
    code = ('text(await tools.exec_command({cmd:"cat /p/BRIEF.md"})); '
            'text(await tools.mcp__rungic_desktop__desktop_goal({goal:"取消 Recover Files 恢复对话框"}));')
    assert router.team.describe_call('exec', code) == ['Read BRIEF.md', '取消 Recover Files 恢复对话框']
    assert router.team.describe_call('send_message', '{"target": "/root"}') == ['Report to the lead']
    assert router.team.describe_call('send_message', '{"target": "/root/art"}') == ['Message art']
    assert router.team.describe_call('wait_agent', '{}') == []


# covers: agent.team/E4
def test_a_chain_is_described_by_its_step_not_its_last_word():
    d = router.team.describe_command
    assert d('mkdir -p .team; printf x > BRIEF.md') == 'Make the folder .team'
    assert d('mkdir -p .team && cat > BRIEF.md <<EOF') == 'Write BRIEF.md'
    assert d('cd ~/Projects/x && python3 check.py') == 'Run check.py'
    assert d("grep -E 'godot|ardour|krita' apps.txt | head") == 'Read apps.txt'


# ---- the board: the whole team at a glance (team.apply_to_board) -----------------------------------
LEAD = 'lead-thread'


def run_board(*entries):
    board = None
    for entry in entries:
        board = router.team.apply_to_board(board, {'time': 1.0, **entry}, '/p')
    return board


# covers: agent.team-board/E1
def test_the_board_follows_a_team_from_brief_to_done():
    b = run_board(
        {'role': 'lead', 'kind': 'brief', 'text': 'Pixel Flappy', 'thread': LEAD, 'workspace': 1},
        {'role': 'art', 'kind': 'review', 'text': '2 points: layers', 'thread': 'a', 'parent': LEAD, 'workspace': 3},
        {'role': 'game', 'kind': 'review', 'text': 'No objections', 'thread': 'g', 'parent': LEAD, 'workspace': 2})
    assert b['phase'] == 'review' and b['title'] == 'Pixel Flappy' and b['lead'] == LEAD
    assert [r['role'] for r in b['reviews']] == ['art', 'game']
    assert [m['role'] for m in b['members']] == ['lead', 'art', 'game'] and b['members'][0]['lead']
    b = router.team.apply_to_board(b, {'role': 'lead', 'kind': 'decision', 'text': 'All accepted', 'thread': LEAD}, '/p')
    assert b['phase'] == 'working' and b['decision'] == 'All accepted'
    assert [m['kind'] for m in b['members']] == ['progress', 'progress', 'progress'], 'all at work once decided'
    b = router.team.apply_to_board(b, {'role': 'art', 'kind': 'review', 'text': '', 'thread': 'a', 'parent': LEAD,
                                       'workspace': 3, 'silent': True}, '')
    assert b['members'][1]['text'] == '2 points: layers', 'a silent update keeps the words'
    b = router.team.apply_to_board(b, {'role': 'lead', 'kind': 'done', 'text': 'Ready to play', 'thread': LEAD}, '/p')
    assert b['phase'] == 'done' and b['result'] == 'Ready to play' and len(b['posts']) == 5


# covers: agent.team-board/E1
def test_a_new_leads_brief_starts_a_new_board():
    b = run_board({'role': 'lead', 'kind': 'brief', 'text': 'One', 'thread': LEAD},
                  {'role': 'lead', 'kind': 'done', 'text': 'Done', 'thread': LEAD},
                  {'role': 'lead', 'kind': 'brief', 'text': 'Two', 'thread': 'other'})
    assert b['title'] == 'Two' and b['phase'] == 'review' and b['result'] == '' and len(b['posts']) == 1


# covers: agent.team-board/E1
def test_a_post_updates_the_board_file(host):
    with mock.patch.object(router, 'Child', FakeChild):
        r = router.Router(WORKSPACE_ENV)
        r.call('team_post', {'role': 'lead', 'kind': 'brief', 'text': 'Card', 'project': str(host / 'card')}, PARENT_META)
    board = json.loads(router.team.board_path().read_text())
    assert board['title'] == 'Card' and board['project'] == str(host / 'card')


# covers: agent.team-board/E1
def test_a_team_whose_lead_stopped_ends(tmp_path):
    # 2026-10-05: a lead's turn cut off (a restart) left its board "working" for good, and the
    # director held its members: one assistant's screen was cast to the TV as four.
    sent = []
    with mock.patch.object(router.team, 'board_path', lambda: tmp_path / 'team-board.json'), \
            mock.patch.object(router.team, '_send', sent.append):
        assert router.team.end(LEAD, 'failed', 'stopped') is None, 'no board, nothing to end'
        board = run_board({'role': 'lead', 'kind': 'brief', 'text': 'Tea', 'thread': LEAD},
                          {'role': 'art', 'kind': 'progress', 'text': 'tiles', 'thread': 'a', 'parent': LEAD})
        (tmp_path / 'team-board.json').write_text(json.dumps(board))
        assert router.team.end('another-lead', 'failed', 'stopped') is None, 'only its own lead ends it'
        ended = router.team.end(LEAD, 'failed', 'The lead stopped')
        assert ended['phase'] == 'failed' and ended['result'] == 'The lead stopped'
        assert json.loads((tmp_path / 'team-board.json').read_text())['phase'] == 'failed'
        assert sent and sent[-1]['board']['phase'] == 'failed', 'the app is told: the director lets go'
        assert router.team.end(LEAD, 'done', 'again') is None, 'an ended board stays as it ended'


# ---- a workspace started again: its child's bus is gone (2026-10-02, the phone dozed) ------------
# covers: agent.workspaces/E8
def test_a_child_on_a_bus_gone_is_replaced(host, tmp_path, monkeypatch):
    bus = tmp_path / 'ws-bus'
    bus.write_text('')
    env = {**WORKSPACE_ENV, 'DBUS_SESSION_BUS_ADDRESS': f'unix:path={bus}'}
    FakeChild.made.clear()
    with mock.patch.object(router, 'Child', FakeChild):
        r = router.Router(env)
        r.call('desktop_windows', {}, PARENT_META)
        r.call('desktop_windows', {}, PARENT_META)
        assert len(FakeChild.made) == 1
        fresh = tmp_path / 'ws-bus.new'       # the workspace's bus made anew (a new inode)
        fresh.write_text('')
        fresh.replace(bus)
        r.call('desktop_windows', {}, PARENT_META)
        assert len(FakeChild.made) == 2


# covers: agent.workspaces/E8
def test_a_closed_connection_is_tried_once_more_with_a_new_child(host, monkeypatch):
    class Stale(FakeChild):
        def request(self, method, params):
            self.calls.append(params['name'])
            if len(FakeChild.made) == 1:
                return {'isError': True, 'content': [{'type': 'text', 'text': 'Error: g-io-error-quark: The connection is closed (18)'}]}
            return {'content': [{'type': 'text', 'text': '{"ok": true}'}]}
    FakeChild.made.clear()
    with mock.patch.object(router, 'Child', Stale):
        r = router.Router(WORKSPACE_ENV)
        result = r.call('desktop_windows', {}, PARENT_META)
    assert len(FakeChild.made) == 2 and not result.get('isError')
