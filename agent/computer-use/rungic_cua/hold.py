"""The user takes over a screen from the agent, and gives it back (docs/114).

The director's "Take over" (agent/screen) writes hold-wsN.json in the screens' runtime directory: from
then on the agent's desktop tools do not act on workspace N (router.py asks `gate` before each call that
acts). A call that comes while the user holds the screen waits; when the user gives the screen back it
does not do what it was asked: the screen changed under the agent, so it says so and the agent looks
again. "Give back" writes handed-wsN.json: how long the user had the screen and what they said, once
for the agent's next call. Looking (screenshots, the window list) goes on all the time.

  hold-wsN.json    {"since": 1790000000.0, "by": "user", "refreshed": 1790000030.0}
  handed-wsN.json  {"since": …, "until": …, "note": "I changed the colour to red"}

A hold the director stopped refreshing (it crashed) ends after STALE_S.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .activity import DIR

STALE_S = 600
WAIT_S = 120.0
# The tools that change a screen. The others only look.
ACTING = ('desktop_act', 'desktop_launch', 'desktop_activate', 'desktop_window', 'desktop_run', 'desktop_goal',
          'desktop_voice_message', 'desktop_voice_recording', 'desktop_close_workspace')


def hold_path(slot) -> Path:
    return DIR / f'hold-ws{slot}.json'


def handed_path(slot) -> Path:
    return DIR / f'handed-ws{slot}.json'


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(temporary, path)


def take(slot, by: str = 'user', clock=time.time) -> dict:
    """The user holds workspace `slot` now (again: refreshed)."""
    current = holder(slot, clock)
    now = clock()
    record = {'since': current['since'] if current else now, 'by': by, 'refreshed': now}
    _write(hold_path(slot), record)
    return record


def give_back(slot, note: str = '', clock=time.time) -> dict | None:
    """The user gives workspace `slot` back -> the handed-back record, or None (it was not held)."""
    current = _read(hold_path(slot))
    hold_path(slot).unlink(missing_ok=True)
    if not current:
        return None
    record = {'since': float(current.get('since') or clock()), 'until': clock(), 'note': note.strip()[:2000]}
    _write(handed_path(slot), record)
    return record


def holder(slot, clock=time.time) -> dict | None:
    record = _read(hold_path(slot))
    if not record:
        return None
    if clock() - float(record.get('refreshed') or record.get('since') or 0) > STALE_S:
        hold_path(slot).unlink(missing_ok=True)
        return None
    return record


def handed(slot) -> dict | None:
    return _read(handed_path(slot))


def back_note(record: dict) -> str:
    seconds = max(0, round(float(record['until']) - float(record['since'])))
    text = (f'The user took over this screen for {seconds} s and gave it back. The screen can be different now. '
            'Take a screenshot and look at it before you act again. Do not undo what the user did.')
    if record.get('note'):
        text += f' The user says: {record["note"]}'
    return text


class Gate:
    """One per tool server (router.py): what its agent was told of holds."""

    def __init__(self, clock=time.time, sleep=time.sleep, wait: float = WAIT_S):
        self.clock, self.sleep, self.wait = clock, sleep, wait
        self.told: dict[int, float] = {}    # workspace -> the last hand-back it was told of ('until')
        self.started = clock()              # hand-backs before this server started are not news

    def check(self, slot: int, name: str) -> str | None:
        """None: go on with `name` on workspace `slot`. A text: do not do it, tell the agent this."""
        news = self._news(slot)
        if name not in ACTING:
            return news                     # looking: done, with the news if there is any
        if news:
            return news
        if not holder(slot, self.clock):
            return None
        deadline = self.clock() + self.wait
        while holder(slot, self.clock) and self.clock() < deadline:
            self.sleep(0.3)
        if holder(slot, self.clock):
            return ('The user has taken over this screen and works on it now. Nothing was done. Do not act on '
                    'this screen until the user gives it back: call the tool again to wait, or do other work.')
        return self._news(slot) or ('The user took over this screen and gave it back. Nothing was done. Take a '
                                    'screenshot and look at it before you act again.')

    def _news(self, slot: int) -> str | None:
        record = handed(slot)
        if not record or float(record.get('until') or 0) <= self.told.get(slot, self.started):
            return None
        self.told[slot] = float(record['until'])
        return back_note(record)
