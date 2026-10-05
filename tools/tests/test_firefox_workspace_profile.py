"""firefox-workspace-profile and the firefox wrapper: an agent workspace's own Firefox profile, with a
copy of the user's sign-ins, so that the phone's Firefox and the workspace's both always open."""
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'desktop/firefox-workspace-profile'
WRAPPER = ROOT / 'desktop/firefox'


def load():
    loader = importlib.machinery.SourceFileLoader('firefox_workspace_profile', str(SCRIPT))
    spec = importlib.util.spec_from_loader('firefox_workspace_profile', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def user_profile(home: Path) -> Path:
    """A user's Firefox under ~/.config/mozilla, as on the phones: the install's default profile,
    with cookies in WAL mode (Firefox writes them so) and saved logins."""
    root = home / '.config/mozilla/firefox'
    profile = root / 'abc.default-release'
    profile.mkdir(parents=True)
    (root / 'profiles.ini').write_text('[Profile0]\nName=default-release\nIsRelative=1\nPath=abc.default-release\n'
                                       '\n[Profile1]\nName=default\nIsRelative=1\nPath=old.default\nDefault=1\n')
    (root / 'old.default').mkdir()
    (root / 'installs.ini').write_text('[4F96D1932A9F858E]\nDefault=abc.default-release\nLocked=1\n')
    db = sqlite3.connect(profile / 'cookies.sqlite')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)')
    db.execute("INSERT INTO moz_cookies VALUES ('.github.com', 'user_session', 'signed-in')")
    db.commit()
    db.close()
    key = sqlite3.connect(profile / 'key4.db')
    key.execute('CREATE TABLE metadata (id TEXT, item1 BLOB)')
    key.execute("INSERT INTO metadata VALUES ('password', x'01')")
    key.commit()
    key.close()
    (profile / 'logins.json').write_text(json.dumps({'logins': [{'hostname': 'https://github.com'}]}))
    return profile


# covers: apps.firefox/E7
def test_the_users_sign_ins_are_copied_into_the_workspace_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('RUNGIC_USER_CONFIG_HOME', raising=False)
    m = load()
    user = user_profile(tmp_path)
    assert m.user_profile(tmp_path, tmp_path / '.config') == user     # the install's default, not Default=1
    target = tmp_path / '.local/state/rungic-workspaces/1/config/mozilla/firefox/rungic-workspace'
    assert m.seed(target, user) == []
    rows = sqlite3.connect(target / 'cookies.sqlite').execute('SELECT value FROM moz_cookies').fetchall()
    assert rows == [('signed-in',)]
    assert json.loads((target / 'logins.json').read_text())['logins'][0]['hostname'] == 'https://github.com'
    assert sqlite3.connect(target / 'key4.db').execute('SELECT count(*) FROM metadata').fetchone() == (1,)
    assert not list(target.glob('*.rungic-part'))


# covers: apps.firefox/E7
def test_a_running_workspace_firefox_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    m = load()
    user_profile(tmp_path)
    target = tmp_path / 'ws'
    target.mkdir()
    (target / 'cookies.sqlite').write_text('in use')
    (target / '.parentlock').touch()
    # Firefox holds a POSIX lock on .parentlock while it runs: another process's, as here.
    holder = subprocess.Popen(['python3', '-c', 'import fcntl,sys,time; f=open(sys.argv[1],"rb+"); '
                               'fcntl.lockf(f, fcntl.LOCK_EX); print("locked", flush=True); time.sleep(30)',
                               str(target / '.parentlock')], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'locked'
        assert m.running(target)
        child = subprocess.run(['python3', str(SCRIPT), str(target)], env=dict(os.environ, HOME=str(tmp_path)))
        assert child.returncode == 0
        assert (target / 'cookies.sqlite').read_text() == 'in use'
    finally:
        holder.kill()
        holder.wait()
    assert not m.running(target)


# covers: apps.firefox/E7
def test_nothing_to_copy_or_a_failure_never_stops_firefox(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / 'empty'))
    assert subprocess.run(['python3', str(SCRIPT), str(tmp_path / 'ws')], env=env).returncode == 0
    broken = tmp_path / 'home'
    profile = user_profile(broken)
    (profile / 'cookies.sqlite').write_bytes(b'not a database')
    for suffix in ('-wal', '-shm'):
        Path(str(profile / 'cookies.sqlite') + suffix).unlink(missing_ok=True)
    child = subprocess.run(['python3', str(SCRIPT), str(tmp_path / 'ws2')], env=dict(env, HOME=str(broken)),
                           capture_output=True, text=True)
    assert child.returncode == 0
    assert 'cookies.sqlite' in child.stderr
    assert (tmp_path / 'ws2/logins.json').exists()      # the rest still copied


def wrapper(tmp_path, env, *args):
    """The wrapper with its two programs swapped for recorders."""
    script = WRAPPER.read_text().replace('/usr/lib/firefox/firefox', str(tmp_path / 'firefox-bin')) \
        .replace('/usr/libexec/rungic-firefox-workspace-profile', str(tmp_path / 'seed'))
    (tmp_path / 'wrapper').write_text(script)
    (tmp_path / 'firefox-bin').write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$OUT"\n')
    (tmp_path / 'seed').write_text('#!/bin/sh\necho "$1" > "$OUT.seed"; exit 3\n')
    for name in ('firefox-bin', 'seed'):
        os.chmod(tmp_path / name, 0o755)
    out = tmp_path / 'out'
    subprocess.run(['sh', str(tmp_path / 'wrapper'), *args], env=dict(os.environ, OUT=str(out), **env), check=True)
    return out.read_text().split('\n')[:-1], (Path(str(out) + '.seed').read_text().strip()
                                               if Path(str(out) + '.seed').exists() else None)


# covers: apps.firefox/E7
def test_the_wrapper_gives_a_workspace_its_own_profile(tmp_path):
    config = tmp_path / 'ws-config'
    args, seeded = wrapper(tmp_path, {'RUNGIC_WORKSPACE': '0', 'XDG_CONFIG_HOME': str(config)}, 'https://example.org')
    profile = str(config / 'mozilla/firefox/rungic-workspace')
    assert args == ['--profile', profile, 'https://example.org']
    assert seeded == profile                       # seeded first; its failure (exit 3) did not stop Firefox


# covers: apps.firefox/E7
def test_the_phone_and_a_named_profile_are_left_alone(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != 'RUNGIC_WORKSPACE'}
    script_env = {'RUNGIC_WORKSPACE': ''}
    args, seeded = wrapper(tmp_path, script_env, 'https://example.org')
    assert args == ['https://example.org'] and seeded is None
    args, seeded = wrapper(tmp_path, {'RUNGIC_WORKSPACE': '2'}, '-P', 'work')
    assert args == ['-P', 'work'] and seeded is None
    assert env is not None
