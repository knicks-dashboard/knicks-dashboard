"""데이터 소스. 대시보드는 Source 인터페이스만 사용한다.

    meta                  -> GameMeta
    current() -> (events, starters, now_elapsed)   지금까지 '보이는' 이벤트
    status()  -> dict     모드, 재생 상태, 배속 등
    control(cmd) -> dict  재생 제어 (라이브는 지원 안 함)

새 데이터 공급원(예: cdn.nba.com 라이브 피드)을 붙일 때는 어댑터에서
GameMeta + list[Event] 를 만들고 이 인터페이스만 구현하면 된다.
"""
from __future__ import annotations

import threading
import time

from . import model as m
from .adapters import stats_v3
from .fetch import load
from .lineups import Starters, infer_from_events

SPEEDS = [1, 5, 10, 30, 60, 120]


class ReplaySource:
    mode = "replay"

    def __init__(self, game_id: str, speed: float = 10, autoplay: bool = True):
        pbp, box = load(game_id)
        self.meta = stats_v3.build_meta(game_id, box)
        self.events = stats_v3.build_events(self.meta, pbp)
        self.starters = infer_from_events(self.meta, self.events)
        self.total = self.events[-1].elapsed
        self.speed = speed
        self.playing = autoplay
        self._sim = 0.0
        self._wall = time.monotonic()
        self._lock = threading.Lock()

    def _tick(self) -> None:
        now = time.monotonic()
        if self.playing:
            self._sim = min(self._sim + (now - self._wall) * self.speed, self.total)
            if self._sim >= self.total:
                self.playing = False
        self._wall = now

    def current(self) -> tuple[list[m.Event], Starters, float]:
        with self._lock:
            self._tick()
            sim = self._sim
        visible = []
        for e in self.events:  # 원본 순서를 지키기 위해 앞에서부터 잘라낸다
            if e.elapsed > sim:
                break
            visible.append(e)
        return visible, self.starters, sim

    def status(self) -> dict:
        with self._lock:
            self._tick()
            return {"mode": self.mode, "playing": self.playing, "speed": self.speed,
                    "speeds": SPEEDS, "elapsed": self._sim, "total": self.total,
                    "final": self._sim >= self.total,
                    "periods": [{"period": p, "label": m.period_label(p),
                                 "start": m.period_start_elapsed(p)} for p in sorted(self.starters)]}

    def control(self, cmd: dict) -> dict:
        with self._lock:
            self._tick()
            action = cmd.get("action")
            if action == "play":
                if self._sim >= self.total:
                    self._sim = 0.0
                self.playing = True
            elif action == "pause":
                self.playing = False
            elif action == "speed":
                self.speed = max(0.25, min(float(cmd["value"]), 600))
            elif action == "seek":
                self._sim = max(0.0, min(float(cmd["value"]), self.total))
            elif action == "restart":
                self._sim, self.playing = 0.0, True
        return self.status()


class StatsLiveSource:
    """진행 중인 경기를 stats.nba.com 에서 주기적으로 받아 온다.

    stats API 는 실시간보다 수십 초 늦을 수 있다. 더 빠른 cdn.nba.com 라이브 피드를
    쓰려면 같은 인터페이스로 어댑터만 바꾸면 된다.
    """
    mode = "live"

    def __init__(self, game_id: str, poll_sec: float = 15):
        self.game_id = game_id
        self.poll_sec = poll_sec
        self._lock = threading.Lock()
        self._last_poll = 0.0
        self.error: str | None = None
        self.events: list[m.Event] = []
        self.starters: Starters = {}
        self._refresh(force=True)

    def _refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_poll < self.poll_sec:
            return
        self._last_poll = time.monotonic()
        try:
            pbp, box = load(self.game_id, refresh=True)
            self.meta = stats_v3.build_meta(self.game_id, box)
            self.events = stats_v3.build_events(self.meta, pbp)
            self.starters = infer_from_events(self.meta, self.events)
            self.error = None
        except Exception as exc:  # 네트워크 오류 시 마지막 데이터를 유지
            self.error = f"{type(exc).__name__}: {exc}"
            if not hasattr(self, "meta"):
                raise

    def current(self) -> tuple[list[m.Event], Starters, float]:
        with self._lock:
            self._refresh()
            now = self.events[-1].elapsed if self.events else 0.0
            return list(self.events), self.starters, now

    def status(self) -> dict:
        final = bool(self.events) and self.events[-1].kind == m.PERIOD_END and \
            self.events[-1].period >= m.REGULATION_PERIODS and _scores_differ(self.events)
        return {"mode": self.mode, "playing": not final, "speed": 1, "speeds": [],
                "elapsed": self.events[-1].elapsed if self.events else 0, "total": None,
                "final": final, "periods": [], "error": self.error, "poll_sec": self.poll_sec}

    def control(self, cmd: dict) -> dict:
        return self.status()


def _scores_differ(events: list[m.Event]) -> bool:
    for e in reversed(events):
        if e.score_home is not None:
            return e.score_home != e.score_away
    return False
