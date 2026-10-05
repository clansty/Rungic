"""rungic_cua.hold (docs/114): the user takes a screen over from the agent in the director, and gives it back."""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'agent/computer-use'))
from rungic_cua import hold, router  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def runtime(tmp_path):
    with mock.patch.object(hold, 'DIR', tmp_path):
        yield tmp_path


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# covers: agent.task-control/E1
def test_looking_goes_on_and_acting_waits_while_the_user_holds_the_screen():
    clock = Clock()
    gate = hold.Gate(clock=clock, sleep=clock.sleep, wait=5)
    hold.take(2, clock=clock)
    assert gate.check(2, 'desktop_screenshot') is None, 'the agent may look'
    note = gate.check(2, 'desktop_act')
    assert 'taken over this screen' in note and 'Nothing was done' in note
    assert clock.now >= 1005, 'it waited first'
    assert gate.check(3, 'desktop_act') is None, 'another screen is not held'


# covers: agent.task-control/E1
def test_given_back_the_agent_looks_again_before_it_acts():
    clock = Clock()
    gate = hold.Gate(clock=clock, sleep=clock.sleep, wait=60)
    hold.take(2, clock=clock)

    def user_gives_back(seconds):
        clock.now += seconds
        if clock.now >= 1012:
            hold.give_back(2, clock=clock)
    gate.sleep = user_gives_back
    note = gate.check(2, 'desktop_act')
    assert 'gave it back' in note and 'Take a screenshot' in note and 'for 12 s' in note
    assert gate.check(2, 'desktop_act') is None, 'told once'


# covers: agent.task-control/E1
def test_a_hand_back_between_calls_is_news_once():
    clock = Clock()
    gate = hold.Gate(clock=clock, sleep=clock.sleep)
    hold.take(1, clock=clock)
    clock.now += 30
    hold.give_back(1, clock=clock)
    assert 'for 30 s' in gate.check(1, 'desktop_screenshot'), 'said with the screenshot'
    assert gate.check(1, 'desktop_act') is None


# covers: agent.task-control/E1
def test_a_hold_nobody_refreshes_ends():
    clock = Clock()
    hold.take(4, clock=clock)
    clock.now += hold.STALE_S + 1
    assert hold.holder(4, clock=clock) is None


# covers: agent.task-control/E1
def test_the_router_does_not_act_on_a_held_screen(runtime):
    calls = []

    class Child:
        def __init__(self, env):
            pass

        def alive(self):
            return True

        def request(self, method, params):
            calls.append(params['name'])
            return {'content': [{'type': 'text', 'text': '{}'}]}
    env = {'RUNGIC_WORKSPACE': '1', 'WAYLAND_DISPLAY': 'wayland-ws-1'}
    with mock.patch.object(router, 'Child', Child), \
            mock.patch.object(router, 'bridge', return_value={'enabled': False, 'tv': False}), \
            mock.patch.object(router, 'desktop_running', lambda: False), \
            mock.patch.object(router.workspace, 'ensure', return_value=True), \
            mock.patch.object(router.workspace, 'thaw', return_value=True), \
            mock.patch.object(router.team, 'Murmur', mock.MagicMock()):
        r = router.Router(env)
        r.gate.wait = 0.2
        hold.take(1)
        result = r.call('desktop_act', {'actions': []})
        assert 'taken over' in result['content'][0]['text'] and calls == []
        r.call('desktop_screenshot', {})
        assert calls == ['desktop_screenshot']
        hold.give_back(1)
        result = r.call('desktop_screenshot', {})
        assert any('gave it back' in c['text'] for c in result['content'])
        r.call('desktop_act', {'actions': []})
        assert calls[-1] == 'desktop_act'
    assert json.loads((runtime / 'handed-ws1.json').read_text())['note'] == ''
