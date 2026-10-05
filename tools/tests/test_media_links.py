"""media.js: links in an agent's answer to files on this phone open as file:// URLs (docs/88)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MEDIA = Path(__file__).resolve().parents[2] / 'agent/assistant/app/qml/media.js'


def run(expression):
    if not shutil.which('node'):
        pytest.skip('node is not installed')
    source = MEDIA.read_text().replace('.pragma library', '')
    script = source + f'\nprocess.stdout.write(JSON.stringify({expression}))\n'
    return json.loads(subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True).stdout)


# covers: agent.attachments/E6
def test_a_bare_or_bracketed_path_with_chinese_opens_as_file_url():
    # The links of an agent's game (2026-10-05): <path> with Chinese, a link the view handed on as is.
    url = '/home/kevinzhow/Shared/Downloads/信阳茶山慢时光.html'
    expected = 'file:///home/kevinzhow/Shared/Downloads/%E4%BF%A1%E9%98%B3%E8%8C%B6%E5%B1%B1%E6%85%A2%E6%97%B6%E5%85%89.html'
    assert run(f'localUrl({json.dumps(url)}, "file:///home/kevinzhow")') == expected
    assert run(f'localUrl({json.dumps("<" + url + ">")}, "file:///home/kevinzhow")') == expected
    assert run('localUrl("~/a b.zip", "file:///home/kevinzhow")') == 'file:///home/kevinzhow/a%20b.zip'
    assert run('localUrl("https://example.org/x", "file:///home/kevinzhow")') == ''


# covers: agent.attachments/E6
def test_a_task_result_gives_its_files_and_pictures():
    text = ('[打开小游戏](</home/k/Shared/Downloads/信阳茶山慢时光.html>)\n'
            '[下载源码包](</home/k/Shared/Downloads/信阳茶山-demo.zip>)\n'
            '![茶山画面预览](</home/k/Pictures/预览.png>)')
    result = run(f'parse({json.dumps(text)}, "file:///home/k")')
    assert [f['name'] for f in result['files']] == ['信阳茶山慢时光.html', '信阳茶山-demo.zip']
    assert [i['name'] for i in result['images']] == ['预览.png']
    assert 'file:///home/k/Shared/Downloads/' in result['text'] and '</home' not in result['text']
