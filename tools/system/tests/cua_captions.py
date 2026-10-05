# SPDX-License-Identifier: MIT
"""The assistant screen's caption, as its floating window reads it (docs/88, docs/research/91): the
window's own AgentScreen (agent/screen/agentscreen.cpp, built here as the window builds it) for
workspaces 1 and 2, fed by rungic-cua's activity reports (rungic_cua.activity, the writer the tools
use). Each workspace's screen shows its own agent's caption, never another's; a "working" caption
whose writer stopped updating it is dropped 120 s after it was written, an ending after a few
seconds. The platform bridge is a stand-in of its contract that says the assistant's screen is on;
the workspaces' KWins are not running (the picture waits, the caption does not). How the window
draws the caption and hides an ending after 4 s is tools/tests/test_cua_caption_qml.py.

(This KWin has no GPU, so no screenshots, and the window's Qt Quick items expose no accessibility tree:
the caption is read from AgentScreen's properties, which the window's QML binds to.)"""
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
SCREEN = {'enabled': True, 'workspace': 1, 'width': 1920, 'height': 1080, 'tv': False, 'tvShown': [],
          'tvHeard': -1, 'directorFocus': 1}
CMAKE = f'''cmake_minimum_required(VERSION 3.22)
project(CaptionProbe LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_AUTOMOC ON)
find_package(Qt6 REQUIRED COMPONENTS Core DBus Gui Network)
qt_add_executable(caption-probe probe.cpp {SRC}/agentscreen.cpp {SRC}/agentscreen.h)
target_include_directories(caption-probe PRIVATE {SRC})
target_link_libraries(caption-probe PRIVATE Qt6::Core Qt6::DBus Qt6::Gui Qt6::Network)
'''
PROBE = r'''#include "agentscreen.h"
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <cstdio>
// Each screen's caption as the floating window's QML reads it: one JSON line per change.
int main(int argc, char **argv)
{
    QGuiApplication app(argc, argv);
    for (int n : {1, 2}) {
        auto *screen = new AgentScreen(n, &app);
        auto say = [screen, n] {
            const QJsonObject line{{QStringLiteral("ws"), n}, {QStringLiteral("state"), screen->activityState()},
                                   {QStringLiteral("text"), screen->activityText()}};
            std::printf("%s\n", QJsonDocument(line).toJson(QJsonDocument::Compact).constData());
            std::fflush(stdout);
        };
        QObject::connect(screen, &AgentScreen::activityChanged, screen, say);
        say();
    }
    return app.exec();
}
'''


def build(tmp):
    (tmp / 'CMakeLists.txt').write_text(CMAKE)
    (tmp / 'probe.cpp').write_text(PROBE)
    log = tmp / 'build.log'
    with open(log, 'w') as out:
        ok = subprocess.run(['cmake', '-S', str(tmp), '-B', str(tmp / 'build')], stdout=out, stderr=out).returncode == 0 \
            and subprocess.run(['cmake', '--build', str(tmp / 'build'), '-j4'], stdout=out, stderr=out).returncode == 0
    if not ok:
        raise harness.Failed('the caption probe did not build: ' + log.read_text()[-1500:])
    return tmp / 'build/caption-probe'


# covers[system]: agent.watch-work/E1 agent.watch-work/E2
def test():
    tmp = Path('/tmp/cua-captions')
    tmp.mkdir(exist_ok=True)
    probe = build(tmp)
    steps = []

    def check(condition, what):
        steps.append(what)
        if not condition:
            raise harness.Failed(what)

    runtime = Path(f'/tmp/rt-captions-{os.getuid()}')
    runtime.mkdir(mode=0o700, exist_ok=True)
    os.environ['XDG_RUNTIME_DIR'] = str(runtime)
    from rungic_cua import activity
    activity.DIR = runtime / 'rungic-agent-screen'
    with contracts.StandIn('platform-bridge', {'agent-screen': SCREEN}) as bridge:
        env = {**os.environ, 'RUNGIC_PLATFORM_SOCKET': bridge.path, 'QT_QPA_PLATFORM': 'offscreen'}
        process = subprocess.Popen([str(probe)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(json.loads(l)) for l in process.stdout], daemon=True).start()
        shown = {1: {}, 2: {}}

        def until(condition, timeout, what):
            deadline = time.monotonic() + timeout
            while not condition():
                try:
                    line = lines.get(timeout=max(0.05, deadline - time.monotonic()))
                    shown[line['ws']] = line
                except queue.Empty:
                    pass
                if time.monotonic() > deadline and not condition():
                    raise harness.Failed(f'timed out waiting for {what}; shown {shown}')
            return time.monotonic()

        try:
            until(lambda: shown[1] and shown[2], 10, 'both screens')
            check(shown[1]['state'] == '' and shown[2]['state'] == '', 'no caption before anyone reports')
            activity.report('打开 Dolphin', workspace=1)
            until(lambda: shown[1].get('text') == '打开 Dolphin', 5, "workspace 1's caption")
            time.sleep(0.5)
            check(shown[1]['state'] == 'working' and shown[2]['state'] == '',
                  "workspace 1's caption is on its screen only")
            activity.report('在 Kate 里输入', workspace=2)
            until(lambda: shown[2].get('text') == '在 Kate 里输入', 5, "workspace 2's caption")
            check(shown[1]['text'] == '打开 Dolphin', "each screen keeps its own agent's caption")
            activity.report('打开 Dolphin', state='done', workspace=1)
            until(lambda: shown[1].get('state') == 'done', 5, 'the ending')
            check(shown[1]['text'] == '打开 Dolphin' and shown[2]['state'] == 'working',
                  "the ending is workspace 1's; workspace 2 still works")
            # A writer gone silent (a tool call killed with Codex): written 116 s ago, gone at 120 s.
            path = activity.path(2)
            report = json.loads(path.read_text())
            report['time'] = time.time() - activity.STALE_S + 4
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(report, ensure_ascii=False))
            os.replace(temporary, path)
            started = time.monotonic()
            gone = until(lambda: shown[2].get('state') == '', 10, 'the stale caption to go')
            check(2.5 <= gone - started <= 7, f'a "working" caption is dropped 120 s after it was written '
                                              f'({gone - started:.1f} s after it was 116 s old)')
            # An ending is news only for a moment: an old one is not shown at all.
            activity.report('再试一次', workspace=2)
            until(lambda: shown[2].get('text') == '再试一次', 5, 'a new caption')
            report.update(state='failed', time=time.time() - 30)
            temporary.write_text(json.dumps(report, ensure_ascii=False))
            os.replace(temporary, path)
            until(lambda: shown[2].get('state') == '', 5, 'the old ending to be dropped')
            check(True, 'an ending written long ago is not shown')
            check(process.poll() is None, 'the screens kept running (the bridge says the screen is on)')
        finally:
            process.kill()
    return steps


if __name__ == '__main__':
    harness.run('cua_captions', test)
