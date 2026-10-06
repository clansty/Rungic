# SPDX-License-Identifier: MIT
"""Reject device and build-host access in offline tests.

Guard tool entry points and absolute remote commands before process creation.
PATH interception records remote commands from child scripts for teardown.
Temporary shell scripts can replace remote programs for transport tests.
Guard failures must escape transfer fallbacks that catch Exception.
See docs/97 and docs/105 for incidents caused by incomplete test isolation.
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def no_device(monkeypatch, tmp_path_factory):
    import rungic_device
    import build_on_device

    def refuse(*args, **kwargs):
        pytest.fail('an offline test reached the device', pytrace=False)
    monkeypatch.setattr(rungic_device, '_run', refuse)
    # Kept for tests that put a fake adb in RUNGIC_ADB and test how the phone is found
    # (tools/tests/test_rungic_agent_mcp.py); they restore it themselves.
    rungic_device.__dict__.setdefault('_unguarded_adb_path', rungic_device.adb_path)
    monkeypatch.setattr(rungic_device, 'adb_path', refuse)

    def refuse_host(*args, **kwargs):
        pytest.fail('an offline test reached the build host', pytrace=False)
    monkeypatch.setattr(build_on_device.MacMini, 'ssh', refuse_host)

    original_popen = subprocess.Popen
    remote_commands = {'adb', 'ssh', 'scp', 'sftp'}
    guard_root = tmp_path_factory.mktemp('offline-guard')
    guarded_bin = guard_root / 'bin'
    guarded_bin.mkdir()
    violations = guard_root / 'external-calls'
    for command in remote_commands:
        script = guarded_bin / command
        script.write_text('#!/bin/sh\n'
                          f'echo {command} >> {shlex.quote(str(violations))}\n'
                          'echo "Offline tests cannot run remote commands." >&2\nexit 97\n')
        script.chmod(0o755)
    monkeypatch.setenv('PATH', str(guarded_bin) + os.pathsep + os.environ['PATH'])

    def check_command(command, env):
        if Path(command).name not in remote_commands:
            return
        resolved = shutil.which(command, path=env.get('PATH'))
        if resolved:
            path = Path(resolved).resolve()
            # Tests can execute their own temporary shell stand-ins.
            if (not path.is_relative_to(guarded_bin) and
                    path.is_relative_to(Path(tempfile.gettempdir()).resolve()) and path.is_file()):
                with path.open('rb') as source:
                    if source.read(2) == b'#!':
                        return
        pytest.fail(f'an offline test reached an external command: {command}', pytrace=False)

    class OfflinePopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            env = kwargs.get('env') or os.environ
            command = args if isinstance(args, (str, bytes)) else args[0]
            for value in (command, kwargs.get('executable')):
                if value and Path(os.fsdecode(value)).is_absolute():
                    check_command(os.fsdecode(value), env)
            super().__init__(args, *positional, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', OfflinePopen)
    yield violations
    if violations.exists():
        pytest.fail('an offline child attempted external commands: ' + violations.read_text().strip(), pytrace=False)
