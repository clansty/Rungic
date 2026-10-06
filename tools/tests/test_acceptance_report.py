# SPDX-License-Identifier: MIT
"""The actual acceptance runner preserves its plan and distinguishes proof from missing work."""
import json
import types
from pathlib import Path

import pytest
import rungic_acceptance as acc
import rungic_release as release_tool


@pytest.fixture
def runner(tmp_path, monkeypatch):
    spec = {'scenarios': [], 'manual': ['Listen to a real recording', 'Check image quality']}
    monkeypatch.setattr(acc, 'RESULTS', tmp_path)
    monkeypatch.setattr(acc, 'load', lambda: spec)
    monkeypatch.setattr(acc, 'bring_to_front', lambda: {'was_in_front': False, 'in_front': True})
    monkeypatch.setattr(acc, 'previous_report', lambda *args: (None, None))
    monkeypatch.setattr(acc.rungic_agent, 'screenshot', lambda: (_ for _ in ()).throw(RuntimeError('no screenshot')))
    observed = []
    def drift(against='origin/main'):
        observed.append(against)
        return {'release': 'installed.1', 'commit': 'installed-sha', 'in_sync': False,
                'differs': [{'part': 'apk', 'state': 'version differs'}], 'against': against}
    monkeypatch.setattr(release_tool, 'phone_drift', drift)
    def run(script, *args, **kwargs):
        text = {'getprop ro.serialno': 'TEST-PHONE\n', 'getprop ro.build.fingerprint': 'test/fingerprint\n',
                'dumpsys battery': '  AC powered: false\n  USB powered: true\n  Wireless powered: false\n  level: 76\n',
                'dumpsys power': 'mWakefulness=Awake\n'}.get(script, '')
        return types.SimpleNamespace(stdout=text, returncode=0)
    monkeypatch.setattr(acc, 'run', run)
    monkeypatch.setitem(acc.CHECKS, 'good', lambda ctx: acc.result(True, {'latency': 3}))
    monkeypatch.setitem(acc.CHECKS, 'bad', lambda ctx: acc.result(False, error='visible failure'))
    def scenario(id, check='good', level='smoke'):
        return {'id': id, 'title': id, 'check': check, 'level': level}
    return types.SimpleNamespace(spec=spec, scenario=scenario, path=tmp_path / 'run', observed=observed)


# covers: delivery.acceptance/E6
@pytest.mark.parametrize('check,skips,verdict,status', [
    ('good', {}, 'pass', 'pass'), ('bad', {}, 'fail', 'fail'),
    ('good', {'one': 'camera unavailable'}, 'incomplete', 'skipped'),
    ('not_implemented', {}, 'incomplete', 'unimplemented')])
def test_verdict_distinguishes_executed_results_from_missing_work(runner, check, skips, verdict, status):
    report = acc.run_scenarios([runner.scenario('one', check)], out_dir=runner.path, skips=skips)
    assert report['verdict'] == verdict
    assert report['scenarios'][0]['status'] == status
    if skips:
        assert report['passed'] is True and report['complete'] is False  # rollback compatibility
    assert report['manual'] == runner.spec['manual']


# covers: delivery.acceptance/E6
@pytest.mark.parametrize('after_one', [False, True])
def test_interrupt_keeps_the_whole_plan_and_does_not_overwrite_passed_rows(runner, monkeypatch, after_one):
    def stop(ctx):
        raise KeyboardInterrupt()
    monkeypatch.setitem(acc.CHECKS, 'stop', stop)
    scenarios = ([runner.scenario('first')] if after_one else []) + [runner.scenario('interrupted', 'stop'), runner.scenario('later')]
    with pytest.raises(KeyboardInterrupt):
        acc.run_scenarios(scenarios, out_dir=runner.path)
    saved = json.loads((runner.path / 'report.json').read_text())
    assert saved['verdict'] == 'incomplete'
    assert [r['id'] for r in saved['scenarios']] == [s['id'] for s in scenarios]
    assert saved['scenarios'][-1]['status'] == 'not-run'
    if after_one:
        assert saved['scenarios'][0]['status'] == 'pass'


# covers: delivery.acceptance/E6
def test_report_exists_before_device_setup_can_fail(runner, monkeypatch):
    monkeypatch.setattr(acc, 'bring_to_front', lambda: (_ for _ in ()).throw(RuntimeError('device disconnected')))
    with pytest.raises(RuntimeError, match='device disconnected'):
        acc.run_scenarios([runner.scenario('one')], out_dir=runner.path)
    saved = json.loads((runner.path / 'report.json').read_text())
    assert saved['verdict'] == 'incomplete' and saved['scenarios'][0]['status'] == 'not-run'
    assert 'device disconnected' in saved['run_error']


# covers: delivery.acceptance/E6
def test_second_run_cannot_destroy_an_existing_report(runner):
    runner.path.mkdir()
    original = '{"original": true}\n'
    (runner.path / 'report.json').write_text(original)
    with pytest.raises(FileExistsError):
        acc.run_scenarios([runner.scenario('one')], out_dir=runner.path)
    assert (runner.path / 'report.json').read_text() == original


# covers: delivery.acceptance/E6
def test_report_records_actual_device_and_pinned_comparison_without_fetching(runner):
    report = acc.run_scenarios([runner.scenario('one')], out_dir=runner.path)
    assert report['system']['release'] == 'installed.1'
    assert report['system']['in_sync'] is False
    assert len(runner.observed) == 1 and len(runner.observed[0]) == 40
    assert report['system']['against'] == runner.observed[0]
    assert report['device']['serial'] == 'TEST-PHONE'
    assert report['device']['fingerprint'] == 'test/fingerprint'
    assert report['device']['battery']['charging'] is True
    assert report['device']['battery']['level'] == 76
    assert report['device']['screen'] == 'Awake'


# R07-R09: full includes actual human observations; smoke explicitly excludes them.
# covers: delivery.acceptance/E6
@pytest.mark.parametrize('scope,manual,verdict', [
    ('smoke', {}, 'pass'), ('full', {}, 'incomplete'),
    ('full', {'manual.1': {'status': 'pass', 'note': 'heard real speech'},
              'manual.2': {'status': 'pass', 'note': 'viewed captured image'}}, 'pass'),
    ('full', {'manual.1': {'status': 'fail', 'note': 'speech inaudible'}}, 'fail')])
def test_full_requires_human_results_but_smoke_does_not_claim_them(runner, scope, manual, verdict):
    report = acc.run_scenarios([runner.scenario('one')], out_dir=runner.path,
                               scope=scope, manual_results=manual)
    assert report['verdict'] == verdict
    assert len(report['manual_results']) == (2 if scope == 'full' else 0)
    assert report['manual'] == runner.spec['manual']
    for id, observation in manual.items():
        assert next(r for r in report['manual_results'] if r['id'] == id)['note'] == observation['note']


# R10/R22: invalid input cannot create a report or touch a device.
# covers: delivery.acceptance/E6
@pytest.mark.parametrize('manual', [
    {'unknown': {'status': 'pass', 'note': 'observed'}},
    {'manual.1': {'status': 'pass', 'note': ''}},
    {'manual.1': {'status': 'invalid', 'note': 'observed'}}])
def test_invalid_manual_input_is_rejected_before_any_execution(runner, manual):
    with pytest.raises(ValueError):
        acc.run_scenarios([runner.scenario('one')], out_dir=runner.path, scope='full', manual_results=manual)
    assert not (runner.path / 'report.json').exists()
    assert not runner.observed


# R20: failed collection stays unknown and never means a zero battery or a matching version.
# covers: delivery.acceptance/E6
def test_metadata_failure_keeps_results_and_records_unknown_environment(runner, monkeypatch):
    def offline(*args, **kwargs):
        raise RuntimeError('phone offline')
    monkeypatch.setattr(acc, 'run', offline)
    monkeypatch.setattr(release_tool, 'phone_drift', offline)
    report = acc.run_scenarios([runner.scenario('one')], out_dir=runner.path)
    assert report['verdict'] == 'incomplete'
    assert report['scenarios'][0]['status'] == 'pass'
    assert report['device'] == {}
    assert 'in_sync' not in report['system']
    assert 'phone offline' in report['metadata_errors']['device']


# R01-R06: actual CLI exit codes use the QA verdict, keeping passed for deploy compatibility.
# covers: delivery.acceptance/E6
@pytest.mark.parametrize('check,skip,exit_code', [('good', [], 0), ('bad', [], 1),
                                               ('good', ['one=excluded'], 2), ('missing', [], 2)])
def test_cli_exit_uses_verdict(runner, monkeypatch, check, skip, exit_code):
    runner.spec['scenarios'] = [runner.scenario('one', check)]
    monkeypatch.setattr('sys.argv', ['acceptance', 'smoke', '--release', 'cli-fixture'] +
                        [arg for entry in skip for arg in ('--skip', entry)])
    assert acc.main() == exit_code


# R10/R22
# covers: delivery.acceptance/E6
@pytest.mark.parametrize('arguments', [
    ['run', 'UNKNOWN'], ['full', '--manual', 'manual.1=pass:observed', '--manual', 'manual.1=fail:other'],
    ['full', '--manual', 'manual.1=pass'], ['full', '--manual', 'manual.1=invalid:observed']])
def test_cli_rejects_unknown_and_contradictory_input(runner, monkeypatch, arguments):
    runner.spec['scenarios'] = [runner.scenario('one')]
    monkeypatch.setattr('sys.argv', ['acceptance', *arguments])
    with pytest.raises(SystemExit) as error:
        acc.main()
    assert error.value.code == 2
    assert not runner.observed
    assert not list(runner.path.parent.rglob('report.json'))


# R20
# covers: delivery.acceptance/E6
def test_empty_device_metadata_is_unknown_with_collection_errors(runner, monkeypatch):
    monkeypatch.setattr(acc, 'run', lambda *args, **kwargs: types.SimpleNamespace(stdout='', returncode=0))
    report = acc.run_scenarios([runner.scenario('one')], out_dir=runner.path)
    assert report['device']['battery']['level'] is None
    assert report['device']['battery']['charging'] is None
    assert report['verdict'] == 'incomplete'
    assert report['metadata_errors']['device']


# R03/R04/R06/R22
# covers: delivery.acceptance/E6
def test_failures_take_priority_over_missing_work_and_exceptions_are_failures(runner, monkeypatch):
    def crash(ctx):
        raise RuntimeError('broken check')
    monkeypatch.setitem(acc.CHECKS, 'crash', crash)
    report = acc.run_scenarios([runner.scenario('A', 'crash'), runner.scenario('B')],
                               out_dir=runner.path, scope='full', skips={'B': 'excluded'})
    assert report['verdict'] == 'fail'
    assert report['counts']['fail'] == 1 and report['counts']['skipped'] == 1
    assert 'broken check' in report['scenarios'][0]['details']['error']
    all_skipped = acc.run_scenarios([runner.scenario('C')], out_dir=runner.path.parent / 'skips', skips={'C': 'excluded'})
    assert all_skipped['verdict'] == 'incomplete'
    with pytest.raises(ValueError, match='no scenarios'):
        acc.run_scenarios([], out_dir=runner.path.parent / 'empty')
    assert not (runner.path.parent / 'empty').exists()


# R13
# covers: delivery.acceptance/E6
def test_interrupt_during_environment_collection_still_has_a_plan(runner, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(release_tool, 'phone_drift', interrupted)
    with pytest.raises(KeyboardInterrupt):
        acc.run_scenarios([runner.scenario('one')], out_dir=runner.path)
    saved = json.loads((runner.path / 'report.json').read_text())
    assert saved['state'] == 'interrupted' and saved['verdict'] == 'incomplete'
    assert saved['counts']['not-run'] == 1


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    root = tmp_path / 'catalog'
    (root / 'quality/features').mkdir(parents=True)
    (root / 'release').mkdir()
    (root / 'quality/features/example.yaml').write_text('''area: example
title: 日常使用
scenarios:
  - {id: first-install, title: 首次安装进入桌面}
  - {id: typing, title: 输入文字}
  - {id: old-rom, title: 旧整包历史}
features:
  - id: typing.android
    title: 用安卓输入法往桌面里打字
    scenario: typing
    status: live
    interfaces: [host-input]
    experience:
      - {id: E1, text: 文字到达获得焦点的输入框}
  - {id: first.install, title: 初始化, scenario: first-install, status: live}
  - {id: old.install, title: 旧安装, scenario: old-rom, status: retired}
''')
    (root / 'quality/interfaces.yaml').write_text('- {id: host-input, title: 宿主输入}\n')
    (root / 'release/history.json').write_text(json.dumps([
        {'version': 'installed.1', 'serial': 'TEST-PHONE', 'result': 'verify-failed', 'time': '2020-01-01T00:00:00+00:00'},
        {'version': 'installed.1', 'serial': 'OTHER-PHONE', 'result': 'ok', 'time': 'other'}]))
    runner_spec = {'scenarios': [
        {'id': 'one', 'title': 'text check', 'level': 'smoke', 'check': 'good', 'covers': ['typing.android/E1']},
        {'id': 'other', 'title': 'other check', 'level': 'full', 'check': 'good', 'covers': ['typing.android/E1']}],
        'manual': ['真人音质']}
    (root / 'release/acceptance.json').write_text(json.dumps(runner_spec))
    monkeypatch.setattr(acc.rungic_device, 'WORKSPACE', root)
    monkeypatch.setattr(acc, 'load', lambda: runner_spec)
    return root


# R21: JSON and readable output stay aligned for pass/fail/missing/manual/interruption.
# covers: delivery.acceptance/E6
@pytest.mark.parametrize('check,scope,skips', [('good', 'smoke', {}), ('bad', 'full', {}),
                                             ('good', 'full', {}), ('good', 'smoke', {'one': 'no camera'})])
def test_render_explains_user_value_scope_and_saved_results_without_touching_device(runner, catalog, monkeypatch,
                                                                                  check, scope, skips):
    report = acc.run_scenarios([runner.scenario('one', check)], out_dir=runner.path, scope=scope, skips=skips)
    source = (runner.path / 'report.json').read_bytes()
    monkeypatch.setattr(acc, 'run', lambda *args, **kwargs: pytest.fail('render touched device'))
    path = acc.render_report(runner.path / 'report.json')
    rendered = path.read_text()
    assert (runner.path / 'report.json').read_bytes() == source
    assert '用安卓输入法往桌面里打字' in rendered
    assert '首次安装进入桌面' in rendered
    assert '旧整包历史' not in rendered
    assert '真人音质' in rendered
    assert 'TEST-PHONE' in rendered and 'test/fingerprint' in rendered
    assert 'verify-failed' in rendered and 'OTHER-PHONE' not in rendered
    assert report['verdict'] in rendered  # machine verdict stays traceable in the appendix
    if skips:
        assert 'no camera' in rendered and '跳过' in rendered
    if scope == 'full':
        assert '待人工' in rendered


# R15/R17/R21: render the last attempt and retain the first failure; no evidence changes.
# covers: delivery.acceptance/E6
@pytest.mark.parametrize('retry_check,retry_skips', [('good', {}), ('good', {'one': 'excluded'}), ('bad', {})])
def test_retry_renderer_reads_initial_failure_and_handles_missing_file(runner, catalog, retry_check, retry_skips):
    first = acc.run_scenarios([runner.scenario('one', 'bad')], out_dir=runner.path)
    second_dir = runner.path.parent / 'retry'
    second = acc.run_scenarios([runner.scenario('one', retry_check)], out_dir=second_dir, skips=retry_skips,
                              retry_of='../run/report.json')
    first_bytes = Path(first['path']).read_bytes()
    second_bytes = Path(second['path']).read_bytes()
    rendered = acc.render_report(second['path']).read_text()
    assert '首次' in rendered and '重试' in rendered and 'visible failure' in rendered
    assert Path(first['path']).read_bytes() == first_bytes and Path(second['path']).read_bytes() == second_bytes
    if retry_check == 'good' and not retry_skips:
        assert '不稳定' in rendered
    Path(first['path']).unlink()
    assert '首次报告缺失' in acc.render_report(second['path']).read_text()


# R21: real CLI rendering works in another process and preserves source bytes.
# covers: delivery.acceptance/E6
def test_render_cli(runner):
    import subprocess
    import sys
    report = acc.run_scenarios([runner.scenario('one')], out_dir=runner.path)
    source = Path(report['path']).read_bytes()
    rendered = subprocess.run([sys.executable, str(Path(acc.__file__)), 'render', report['path']],
                              capture_output=True, text=True, timeout=30)
    assert rendered.returncode == 0, rendered.stderr
    assert (runner.path / 'report.md').exists()
    assert Path(report['path']).read_bytes() == source


# R21: interruption must retain its cause in the human report, not just a partial count.
# covers: delivery.acceptance/E6
def test_renderer_shows_interruption_reason_and_full_manual_observations(runner, catalog, monkeypatch):
    def stop(ctx):
        raise KeyboardInterrupt('operator stopped run')
    monkeypatch.setitem(acc.CHECKS, 'stop', stop)
    with pytest.raises(KeyboardInterrupt):
        acc.run_scenarios([runner.scenario('one', 'stop')], out_dir=runner.path)
    rendered = acc.render_report(runner.path / 'report.json').read_text()
    assert 'operator stopped run' in rendered and '未执行' in rendered
    manual_dir = runner.path.parent / 'human'
    manual = {'manual.1': {'status': 'fail', 'note': '真实语音听不清'}}
    report = acc.run_scenarios([runner.scenario('one')], out_dir=manual_dir, scope='full', manual_results=manual)
    rendered = acc.render_report(report['path']).read_text()
    assert '真实语音听不清' in rendered and 'manual.1' in rendered and '人工失败' in rendered


# Honest metrics and scope: collecting timings alone does not prove absence of regression.
# covers: delivery.acceptance/E6
def test_renderer_labels_missing_performance_reference_and_plan_scope(runner):
    report = acc.run_scenarios([runner.scenario('perf.compositor')], out_dir=runner.path, scope='full',
        manual_results={f'manual.{i}': {'status': 'pass', 'note': 'observed'} for i in (1, 2)})
    rendered = acc.render_report(report['path']).read_text()
    assert '无参考，未比较' in rendered
    assert '本次完整检查计划通过' in rendered
    assert '不包含首次安装、整机重启、长时间待机' in rendered
