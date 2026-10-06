#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the actual Android KWin backend without Android or a GPU.

First start without any host compositor, open an unsaved Qt editor, then connect,
disconnect and reconnect a private headless Wayland host. Nothing touches a user
session. Set RUNGIC_KWIN_BINARY to test a freshly built patch queue binary.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

CLIENT = r'''
#include <QApplication>
#include <QPlainTextEdit>
#include <QDBusConnection>
#include <QJsonDocument>
#include <QJsonObject>
#include <QScreen>
class Probe : public QObject {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.rungic.ColdProbe")
public:
    QPlainTextEdit editor;
    Probe() {
        editor.setWindowTitle("Rungic cold boot unsaved editor");
        editor.setPlainText("UNSAVED-COLD-BOOT");
        editor.resize(300, 400);
        editor.show();
    }
public slots:
    QString state() {
        auto s = editor.screen();
        return QString::fromUtf8(QJsonDocument(QJsonObject{
            {"text", editor.toPlainText()}, {"screen", s ? s->name() : ""},
            {"width", s ? s->geometry().width() : 0}, {"height", s ? s->geometry().height() : 0}
        }).toJson(QJsonDocument::Compact));
    }
};
int main(int argc, char **argv) {
    QApplication a(argc, argv);
    Probe p;
    auto bus = QDBusConnection::sessionBus();
    if (!bus.registerService("org.rungic.ColdProbe") ||
        !bus.registerObject("/Probe", &p, QDBusConnection::ExportAllSlots)) return 1;
    return a.exec();
}
#include "probe.moc"
'''


def test():
    children = []
    logs = []
    buses = []
    kwin = os.environ.get('RUNGIC_KWIN_BINARY', 'kwin_wayland')
    qdbus = shutil.which('qdbus6') or shutil.which('qdbus')
    if not qdbus:
        raise AssertionError('qdbus is required')
    with tempfile.TemporaryDirectory(prefix='rungic-cold-') as directory:
        root = Path(directory)
        root.chmod(0o700)
        source = root / 'probe-src'
        source.mkdir()
        (source / 'probe.cpp').write_text(CLIENT)
        (source / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.22)
project(ColdProbe LANGUAGES CXX)
set(CMAKE_AUTOMOC ON)
find_package(Qt6 REQUIRED COMPONENTS Widgets DBus)
add_executable(probe probe.cpp)
target_link_libraries(probe Qt6::Widgets Qt6::DBus)
''')
        subprocess.run(['cmake', '-S', str(source), '-B', str(root / 'probe-build')], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run(['cmake', '--build', str(root / 'probe-build'), '-j2'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def bus():
            result = subprocess.run(['dbus-daemon', '--session', '--fork', '--print-address=1', '--print-pid=1'],
                                    capture_output=True, text=True, check=True, timeout=5)
            address, pid = result.stdout.strip().splitlines()
            buses.append(int(pid))
            return address

        nested_env = {**os.environ, 'XDG_RUNTIME_DIR': str(root), 'DBUS_SESSION_BUS_ADDRESS': bus(),
                      'KWIN_COMPOSE': 'Q', 'LIBGL_ALWAYS_SOFTWARE': '1',
                      'QT_QPA_PLATFORM': 'wayland', 'WAYLAND_DISPLAY': 'client',
                      'QT_LOGGING_RULES': 'kwin_wayland_backend.info=true'}
        nested_env.pop('DISPLAY', None)
        host_env = {**nested_env, 'DBUS_SESSION_BUS_ADDRESS': bus()}
        host_env.pop('WAYLAND_DISPLAY', None)

        def start(args, env, name):
            f = open(root / f'{name}.log', 'w')
            logs.append(f)
            p = subprocess.Popen(args, env=env, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
            children.append(p)
            return p

        def wait(check, description, seconds=15):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if check():
                    return
                if any(p.poll() is not None for p in children if p not in ended):
                    raise AssertionError('process exited while waiting for ' + description + ': ' + str([(p.args[0], p.poll()) for p in children]))
                time.sleep(0.1)
            raise AssertionError('timeout waiting for ' + description)

        def query(service, obj, method):
            done = subprocess.run([qdbus, service, obj, method], env=nested_env,
                                  capture_output=True, text=True, timeout=3)
            return done.stdout.strip() if done.returncode == 0 else ''

        ended = set()
        flags = ['--no-lockscreen', '--no-global-shortcuts', '--no-kactivities', '--width=720', '--height=1600']
        try:
            # covers[system]: install.independent-runtime/E5
            nested = start([kwin, '--android-host', '--android-render-device=/no-gpu',
                            f'--wayland-display={root}/host', '--socket=client', *flags], nested_env, 'nested')
            wait(lambda: (root / 'client').is_socket(), 'cold compositor socket')
            probe = start([str(root / 'probe-build/probe')], nested_env, 'probe')
            wait(lambda: 'UNSAVED-COLD-BOOT' in query('org.rungic.ColdProbe', '/Probe', 'org.rungic.ColdProbe.state'),
                 'unsaved window before any host exists')
            initial = json.loads(query('org.rungic.ColdProbe', '/Probe', 'org.rungic.ColdProbe.state'))
            assert initial['width'] > 0 and initial['height'] > 0, initial
            before = query('org.kde.KWin', '/KWin', 'org.kde.KWin.supportInformation')
            assert 'Number of Screens: 1' in before and 'Name: WL-0' in before and 'Enabled: 1' in before, before
            assert f"Geometry: 0,0,{initial['width']}x{initial['height']}" in before, before
            steps = ['actual Android backend starts without host; unsaved Qt editor has a valid screen']
            for cycle in range(2):
                host = start([kwin, '--virtual', '--socket=host', *flags], host_env, f'host-{cycle}')
                wait(lambda: (root / 'host').is_socket(), 'host socket')
                wait(lambda: (root / 'nested.log').read_text().count('Reconnected to the Android host') >= cycle + 1,
                     'first host attachment' if cycle == 0 else 'host reattachment')
                assert nested.poll() is None and probe.poll() is None
                state = json.loads(query('org.rungic.ColdProbe', '/Probe', 'org.rungic.ColdProbe.state'))
                assert state['text'] == initial['text'], state
                host.terminate()
                host.wait(timeout=10)
                ended.add(host)
                # Clean only this test's host socket; a killed compositor can leave its pathname.
                (root / 'host').unlink(missing_ok=True)
                wait(lambda: (root / 'nested.log').read_text().count('Android host connection lost') >= cycle + 1,
                     'host loss')
                assert nested.poll() is None and probe.poll() is None
                assert json.loads(query('org.rungic.ColdProbe', '/Probe', 'org.rungic.ColdProbe.state'))['text'] == initial['text']
                steps.append(f'cycle {cycle + 1}: same compositor/editor processes and unsaved text after attach and loss')
            return steps
        except Exception as error:
            evidence = '\n'.join(f'{p.name}: {p.read_text()[-4000:]}' for p in root.glob('*.log'))
            raise AssertionError(f'{error}\n{evidence}') from error
        finally:
            for p in reversed(children):
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=5)
            for pid in buses:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for f in logs:
                f.close()


if __name__ == '__main__':
    try:
        import harness
    except ImportError:
        print(json.dumps({'test': 'desktop_without_host', 'passed': True, 'steps': test()}))
    else:
        harness.run('desktop_without_host', test)
