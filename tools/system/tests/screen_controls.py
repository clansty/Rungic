# SPDX-License-Identifier: MIT
"""The director's controls of the work on a screen (docs/114), as the window's AgentScreen
(agent/screen/agentscreen.cpp, built here as the window builds it) does them against the voice agent's
D-Bus methods: ScreenWork tells it a task works on workspace 2; a press on the screen while the task
works takes the screen over (HoldScreen, rungic_cua.hold's file); Tell sends the words (SteerScreen);
Give back sends the user's words with it; Stop is said "stopped" only from the answer. The voice agent
is a stand-in of those methods on a private session bus; the platform bridge a stand-in of its contract.
(No GPU here: the window's QML is not drawn; AgentScreen's properties are what it binds to.)"""
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import contracts
import harness

SRC = Path('/src/agent/screen')
SCREEN = {'enabled': True, 'workspace': 2, 'width': 1920, 'height': 1080, 'tv': False, 'tvShown': [],
          'tvHeard': -1, 'directorFocus': 2}
CMAKE = f'''cmake_minimum_required(VERSION 3.22)
project(ControlsProbe LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_AUTOMOC ON)
find_package(Qt6 REQUIRED COMPONENTS Core DBus Gui Network)
qt_add_executable(controls-probe probe.cpp {SRC}/agentscreen.cpp {SRC}/agentscreen.h)
target_include_directories(controls-probe PRIVATE {SRC})
target_link_libraries(controls-probe PRIVATE Qt6::Core Qt6::DBus Qt6::Gui Qt6::Network)
'''
# Workspace 2's AgentScreen; what it shows, one JSON line per change; commands on stdin.
PROBE = r'''#include "agentscreen.h"
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSocketNotifier>
#include <cstdio>
#include <iostream>
#include <unistd.h>
int main(int argc, char **argv)
{
    QGuiApplication app(argc, argv);
    auto *screen = new AgentScreen(2, &app);
    auto say = [screen] {
        const QJsonObject line{{"kind", screen->workKind()}, {"busy", screen->workBusy()}, {"held", screen->held()},
                               {"heldHere", screen->heldHere()}};
        std::printf("%s\n", QJsonDocument(line).toJson(QJsonDocument::Compact).constData());
        std::fflush(stdout);
    };
    QObject::connect(screen, &AgentScreen::workChanged, screen, say);
    QObject::connect(screen, &AgentScreen::controlDone, screen, [](const QString &what, bool ok, const QString &detail) {
        const QJsonObject line{{"done", what}, {"ok", ok}, {"detail", detail}};
        std::printf("%s\n", QJsonDocument(line).toJson(QJsonDocument::Compact).constData());
        std::fflush(stdout);
    });
    say();
    auto *input = new QSocketNotifier(STDIN_FILENO, QSocketNotifier::Read, &app);
    QObject::connect(input, &QSocketNotifier::activated, &app, [screen, &app] {
        std::string line;
        if (!std::getline(std::cin, line)) { app.quit(); return; }
        const QString command = QString::fromStdString(line);
        if (command == "press") { screen->pointerButton(1, true); screen->pointerButton(1, false); }
        else if (command.startsWith("tell ")) screen->tell(command.mid(5));
        else if (command == "stop") screen->stopWork();
        else if (command.startsWith("back ")) screen->giveBack(command.mid(5));
        else if (command == "quit") app.quit();
    });
    return app.exec();
}
'''
# The voice agent's methods (rungic_voice_agent.py), as a stand-in: the calls are recorded.
AGENT = r'''
import json, os, sys
sys.path.insert(0, '/src/agent/computer-use')
from rungic_cua import hold
import dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
calls = open(os.environ['CALLS'], 'a', buffering=1)
class Agent(dbus.service.Object):
    @dbus.service.method('com.rungic.VoiceAgent', in_signature='i', out_signature='s')
    def ScreenWork(self, slot):
        return json.dumps({'workspace': int(slot), 'kind': 'side', 'thread': 'th', 'busy': True,
                           'held': bool(hold.holder(int(slot))), 'role': ''})
    @dbus.service.method('com.rungic.VoiceAgent', in_signature='is', out_signature='s')
    def SteerScreen(self, slot, text):
        calls.write(json.dumps(['SteerScreen', int(slot), str(text)]) + '\n')
        return json.dumps({'outcome': 'steered', 'turn': 't1'})
    @dbus.service.method('com.rungic.VoiceAgent', in_signature='i', out_signature='s')
    def StopScreen(self, slot):
        calls.write(json.dumps(['StopScreen', int(slot)]) + '\n')
        return json.dumps({'stopped': True, 'turn': 't1'})
    @dbus.service.method('com.rungic.VoiceAgent', in_signature='ibs', out_signature='s')
    def HoldScreen(self, slot, on, note):
        calls.write(json.dumps(['HoldScreen', int(slot), bool(on), str(note)]) + '\n')
        if on:
            hold.take(int(slot))
        else:
            hold.give_back(int(slot))
        return json.dumps({'held': bool(on)})
name = dbus.service.BusName('com.rungic.VoiceAgent', dbus.SessionBus())
Agent(dbus.SessionBus(), '/com/rungic/VoiceAgent')
print('ready', flush=True)
GLib.MainLoop().run()
'''


def build(tmp):
    (tmp / 'CMakeLists.txt').write_text(CMAKE)
    (tmp / 'probe.cpp').write_text(PROBE)
    log = tmp / 'build.log'
    with open(log, 'w') as out:
        ok = subprocess.run(['cmake', '-S', str(tmp), '-B', str(tmp / 'build')], stdout=out, stderr=out).returncode == 0 \
            and subprocess.run(['cmake', '--build', str(tmp / 'build'), '-j4'], stdout=out, stderr=out).returncode == 0
    if not ok:
        raise harness.Failed('the controls probe did not build: ' + log.read_text()[-1500:])
    return tmp / 'build/controls-probe'


# covers[system]: agent.task-control/E1 agent.task-control/E5
def test():
    tmp = Path('/tmp/screen-controls')
    tmp.mkdir(exist_ok=True)
    probe = build(tmp)
    steps = []

    def check(condition, what):
        steps.append(what)
        if not condition:
            raise harness.Failed(what)

    runtime = Path(f'/tmp/rt-controls-{os.getuid()}')
    runtime.mkdir(mode=0o700, exist_ok=True)
    os.environ['XDG_RUNTIME_DIR'] = str(runtime)
    calls_path = tmp / 'calls.jsonl'
    calls_path.write_text('')
    (tmp / 'agent.py').write_text(AGENT)
    bus = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address'], stdout=subprocess.PIPE, text=True)
    address = bus.stdout.readline().strip()
    env = {**os.environ, 'DBUS_SESSION_BUS_ADDRESS': address, 'CALLS': str(calls_path)}
    agent = subprocess.Popen(['python3', str(tmp / 'agent.py')], stdout=subprocess.PIPE, text=True, env=env)
    process = None
    try:
        check(agent.stdout.readline().strip() == 'ready', 'the stand-in voice agent is on the bus')
        with contracts.StandIn('platform-bridge', {'agent-screen': SCREEN}) as bridge:
            process = subprocess.Popen([str(probe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True, env={**env, 'RUNGIC_PLATFORM_SOCKET': bridge.path, 'QT_QPA_PLATFORM': 'offscreen'})
            lines = queue.Queue()
            threading.Thread(target=lambda: [lines.put(json.loads(l)) for l in process.stdout], daemon=True).start()
            shown, done = {}, []

            def until(condition, timeout, what):
                deadline = time.monotonic() + timeout
                while not condition():
                    try:
                        line = lines.get(timeout=max(0.05, deadline - time.monotonic()))
                        (done.append(line) if 'done' in line else shown.update(line))
                    except queue.Empty:
                        pass
                    if time.monotonic() > deadline and not condition():
                        raise harness.Failed(f'timed out waiting for {what}; shown {shown}, done {done}')

            def calls():
                return [json.loads(l) for l in calls_path.read_text().splitlines()]

            def send(command):
                process.stdin.write(command + '\n')
                process.stdin.flush()

            until(lambda: shown.get('busy'), 10, 'the task at work on the screen')
            check(shown['kind'] == 'side' and not shown['held'], 'the screen knows a task works on it, not held')
            send('press')
            until(lambda: shown.get('held') and shown.get('heldHere'), 10, 'the take-over')
            check(calls()[0] == ['HoldScreen', 2, True, ''], 'a press while the task works takes the screen over')
            check((runtime / 'rungic-agent-screen/hold-ws2.json').exists(), "the agent's tools see the hold")
            send('press')
            time.sleep(1)
            check([c[0] for c in calls()].count('HoldScreen') == 1, 'a second press does not ask again')
            send('tell 把天空改成红色')
            until(lambda: any(d['done'] == 'tell' for d in done), 10, 'the words to go')
            check(['SteerScreen', 2, '把天空改成红色'] in calls() and next(d for d in done if d['done'] == 'tell')['detail'] == 'steered',
                  'Tell sends the words for this screen and says they reached the task')
            send('back 我把颜色调好了')
            until(lambda: not shown.get('held'), 10, 'the hand-back')
            check(['HoldScreen', 2, False, '我把颜色调好了'] in calls(), 'Give back sends the user\'s words with it')
            check(not (runtime / 'rungic-agent-screen/hold-ws2.json').exists(), 'the hold is gone')
            send('stop')
            until(lambda: any(d['done'] == 'stop' for d in done), 10, 'the stop')
            check(next(d for d in done if d['done'] == 'stop')['ok'] is True and ['StopScreen', 2] in calls(),
                  'Stop is said done from the answer that the task stopped')
            send('quit')
            process.wait(10)
    finally:
        for p in (process, agent, bus):
            if p and p.poll() is None:
                p.kill()
    return steps


if __name__ == '__main__':
    harness.run('screen_controls', test)
