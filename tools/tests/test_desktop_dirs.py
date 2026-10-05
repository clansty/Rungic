"""rungic-desktop-dirs: the independent desktop's view of the user's files (docs/research/97 §19.2)."""
import importlib.machinery
import importlib.util
import os
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'agent/workspace/rungic-desktop-dirs'


def load(tmp_path, monkeypatch):
    monkeypatch.setenv('RUNGIC_USER_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('RUNGIC_USER_DATA_HOME', str(tmp_path / 'share'))
    loader = importlib.machinery.SourceFileLoader('rungic_desktop_dirs', str(SCRIPT))
    spec = importlib.util.spec_from_loader('rungic_desktop_dirs', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def setup(tmp_path):
    (tmp_path / 'config/plasma-mobile').mkdir(parents=True)
    (tmp_path / 'config/kdeglobals').write_text('[General]\n')
    (tmp_path / 'config/kwinrc').write_text('[Phone]\n')
    (tmp_path / 'config/dolphinrc').write_text('[Dolphin]\n')
    (tmp_path / 'share/klipper').mkdir(parents=True)
    (tmp_path / 'share/applications').mkdir(parents=True)
    return tmp_path / 'state'


# covers: desktop-mode.independent-desktop/E2 desktop-mode.clipboard/E3
def test_shared_entries_are_links_and_private_ones_absent(tmp_path, monkeypatch):
    d = load(tmp_path, monkeypatch)
    state = setup(tmp_path)
    d.build(state)
    assert (state / 'config/kdeglobals').resolve() == tmp_path / 'config/kdeglobals'
    assert (state / 'config/dolphinrc').is_symlink()
    assert not os.path.lexists(state / 'config/kwinrc')            # KWin's: the desktop's own
    assert not os.path.lexists(state / 'config/plasma-mobile')     # the phone's layer
    assert (state / 'data/applications').is_symlink()
    assert not os.path.lexists(state / 'data/klipper')             # its clipboard history apart


# covers: desktop-mode.independent-desktop/E2
def test_a_file_made_on_the_desktop_becomes_the_users_once_left_alone(tmp_path, monkeypatch):
    d = load(tmp_path, monkeypatch)
    state = setup(tmp_path)
    d.build(state)
    made = state / 'config/kritarc'
    made.write_text('[Krita]\n')
    assert d.step(state) == []                                     # a moment old: maybe still being saved
    old = time.time() - 60
    os.utime(made, (old, old))
    assert d.step(state) == ['shared config/kritarc']
    assert (tmp_path / 'config/kritarc').read_text() == '[Krita]\n'
    assert made.is_symlink()


# covers: desktop-mode.independent-desktop/E2
def test_private_and_lock_files_stay(tmp_path, monkeypatch):
    d = load(tmp_path, monkeypatch)
    state = setup(tmp_path)
    d.build(state)
    old = time.time() - 60
    for name in ('kwinrc', 'plasmashellrc', 'dolphinrc.lock'):
        (state / 'config' / name).write_text('x')
        os.utime(state / 'config' / name, (old, old))
    assert d.step(state) == []
    assert (tmp_path / 'config/kwinrc').read_text() == '[Phone]\n'


# covers: desktop-mode.independent-desktop/E2
def test_the_phones_new_and_removed_files_follow(tmp_path, monkeypatch):
    d = load(tmp_path, monkeypatch)
    state = setup(tmp_path)
    d.build(state)
    (tmp_path / 'config/okularrc').write_text('[Okular]\n')
    (tmp_path / 'config/dolphinrc').unlink()
    done = d.step(state)
    assert 'linked config/okularrc' in done and 'unlinked config/dolphinrc' in done
    assert (state / 'config/okularrc').is_symlink()
    assert not os.path.lexists(state / 'config/dolphinrc')


# covers: desktop-mode.independent-desktop/E2
def test_rebuilding_keeps_the_private_files(tmp_path, monkeypatch):
    d = load(tmp_path, monkeypatch)
    state = setup(tmp_path)
    d.build(state)
    (state / 'config/kwinrc').write_text('[Desktop]\n')
    d.build(state)
    assert (state / 'config/kwinrc').read_text() == '[Desktop]\n'
    assert (state / 'config/kdeglobals').is_symlink()


# covers: apps.firefox/E7
def test_firefox_profiles_are_the_desktops_own(tmp_path, monkeypatch):
    d = load(tmp_path, monkeypatch)
    state = setup(tmp_path)
    (tmp_path / 'config/mozilla/firefox/abc.default-release').mkdir(parents=True)
    # A desktop set up before (2026-10-05) linked the user's; build drops that link.
    (state / 'config').mkdir(parents=True)
    (state / 'config/mozilla').symlink_to(tmp_path / 'config/mozilla')
    d.build(state)
    assert not os.path.lexists(state / 'config/mozilla')
    # The desktop's Firefox makes its own; watch leaves it there, and does not link the user's.
    (state / 'config/mozilla/firefox/rungic-workspace').mkdir(parents=True)
    os.utime(state / 'config/mozilla', (time.time() - 60, time.time() - 60))
    d.step(state)
    assert not (state / 'config/mozilla').is_symlink()
    assert (tmp_path / 'config/mozilla/firefox/abc.default-release').is_dir()
    assert not (tmp_path / 'config/mozilla/firefox/rungic-workspace').exists()
