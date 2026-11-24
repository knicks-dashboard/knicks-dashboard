"""닉스 프리시즌 라이브 추적 테스트 도구 (정식 데이터와 완전히 분리).

모든 쓰기는 test_live/ 안에서만 일어난다. 지난 시즌 맞대결 비교를 위해
baseline/game_log.csv 를 읽기 전용으로 열 뿐, baseline/ 에는 아무것도 쓰지 않는다.

  라이브:   python3 test_live/tracker.py live --lan            (닉스의 오늘/다음 경기를 자동으로 찾음)
            python3 test_live/tracker.py live --game 0012600035 --lan   (경기 직접 지정)
  리플레이: python3 test_live/tracker.py replay --speed 10 --lan      (가장 최근에 끝난 닉스 경기)
            python3 test_live/tracker.py replay --vs PHI / --date 10/5 / --game 0012600023
            python3 test_live/tracker.py replay --list                  (최근 경기 목록)
  간격 분석: python3 test_live/tracker.py report test_live/logs/<게임>/<실행시각>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nba_http as NH  # noqa: E402  (같은 폴더 · NBA 요청 공통 세션)

NH.install()
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_ROOT = os.path.join(HERE, "logs")
LIVE_LAST = os.path.join(HERE, "cache", "live_last")   # 정식 라이브의 마지막 성공 응답 (재시작 후 복원용, 리플레이는 쓰지 않음)
REPLAY_DATA = os.path.join(HERE, "replay_data")
WEB = os.path.join(HERE, "web")
KST = timezone(timedelta(hours=9))
FOCUS = "NYK"
RECENT_PLAYS = 40   # 문자중계용 (화면에서 스크롤)
STOP_AFTER_FINAL_POLLS = 2
LIVE_POLL_SEC = 30          # 경기 중 stats.nba.com(점수만) 갱신 간격 (경기 전·후는 기본 간격)
# 경기 중 갱신 간격은 출처별로 (2026-11-03): ESPN 10초, cdn.nba.com 15초 (다시 쓰게 되면)
CDN_LIVE_SEC = 15
ESPN_LIVE_SEC = (10, 20, 30)   # ESPN 이 429·5xx 를 주거나 3초 넘게 걸리면 한 단계씩 늘린다
ESPN_SLOW_MS = 3000
ESPN_CALM_SEC = 600            # 10분 동안 문제가 없으면 다시 10초로
BASELINE_GAME_LOG = os.path.join(os.path.dirname(HERE), "baseline", "game_log.csv")  # 읽기 전용
H2H_CACHE = os.path.join(HERE, "h2h_cache")

# 알림 기준
RUN_ALERT_PTS = 6            # 상대 무득점 연속 득점
CLUTCH_CLOCK_SEC = 5 * 60    # 4쿼터(또는 연장) 남은 시간
CLUTCH_MARGIN = 5            # 점수 차
# 역전 알림: 1~2쿼터 없음, 3쿼터는 한 번 보낸 뒤 경기 시간 3분 동안 쉼, 4쿼터·연장은 모두
LEAD_ALERT_FROM_PERIOD = 3
LEAD_Q3_COOLDOWN = 3 * 60
MAX_NOTIFY_PER_POLL = 3

# cdn.nba.com 은 브라우저가 아닌 요청을 403 으로 막는다 (봇 차단).
# User-Agent 와 Referer 가 필수이고, 나머지도 함께 보내야 안정적으로 통과했다 (2026-10-09 확인: 24/24 성공).
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

_ISO_CLOCK = re.compile(r"PT(\d+)M([\d.]+)S")
_AST = re.compile(r"\(([^()]+) \d+ AST\)")


# ---------------------------------------------------------------- 공통 유틸

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def clock_sec(s: str | None) -> float:
    m = _ISO_CLOCK.match(s or "")
    return int(m.group(1)) * 60 + float(m.group(2)) if m else 0.0


def clock_sec_label(label: str | None) -> float:
    """화면용 시계 문자열('5:31', '36.1', '0.0') → 초."""
    if not label:
        return 0.0
    if ":" in label:
        m, s = label.split(":")
        return int(m) * 60 + float(s)
    try:
        return float(label)
    except ValueError:
        return 0.0


def fmt_clock(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}"
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def elapsed(period: int, clock: float) -> float:
    if period <= 4:
        return (period - 1) * 720 + 720 - clock
    return 2880 + (period - 5) * 300 + 300 - clock


def period_label(p: int) -> str:
    return f"Q{p}" if p <= 4 else f"OT{p - 4}"


def to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 데이터 소스
# 각 소스의 fetch() 는 {"source": 이름, "format": "live"|"stats", "endpoints": {이름: 결과}} 를 돌려준다.
# 결과는 {"ok": bool, "data": dict | None, "error": str | None, "ms": int}

def _call(fn) -> dict:
    t0 = time.monotonic()
    try:
        return {"ok": True, "data": fn(), "error": None, "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as exc:  # 403, 경기 전 404, JSON 오류 등
        return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "ms": int((time.monotonic() - t0) * 1000)}


CDN_LIVE = "https://cdn.nba.com/static/json/liveData/"


def cdn_get(path: str, purpose: str | None = None) -> dict:
    """cdn.nba.com live JSON. purpose="live" 는 라이브 추적 중인 닉스 경기 요청에만 (요청 관리자가 그것만 보낸다).
    공통 세션(nba_http: 브라우저 헤더 · 연결 5초/읽기 15초 · 호스트별 백오프)으로 한 번만 보낸다.
    실패하면 다시 묻지 않는다 — 막혔을 때 같은 요청을 거듭 보내면 차단이 길어진다."""
    try:
        return NH.get_json(CDN_LIVE + path, {"Cache-Control": "no-cache"}, purpose=purpose)
    except NH.BackedOff:
        raise
    except Exception as exc:
        raise RuntimeError(f"cdn.nba.com {path.split('/')[0]} {str(exc).replace('cdn.nba.com ', '')[:120]}") from None


def box_as_scoreboard(b: dict) -> dict:
    """라이브 박스스코어로 스코어보드 정보를 만든다 (스코어보드만 실패할 때)."""
    return {"gameId": b["gameId"], "gameStatus": b["gameStatus"], "gameStatusText": b.get("gameStatusText", ""),
            "period": b.get("period"), "gameClock": b.get("gameClock"), "gameTimeUTC": b.get("gameTimeUTC"),
            "homeTeam": {"teamTricode": b["homeTeam"]["teamTricode"], "score": b["homeTeam"].get("score")},
            "awayTeam": {"teamTricode": b["awayTeam"]["teamTricode"], "score": b["awayTeam"].get("score")}}


class LiveApiSource:
    """cdn.nba.com live 엔드포인트 (nba_api live 와 같은 주소) + 브라우저 헤더."""
    name, fmt = "nba_api.live", "live"

    def __init__(self, game_id: str):
        self.game_id = game_id

    def fetch(self) -> dict:
        def sb():
            games = cdn_get("scoreboard/todaysScoreboard_00.json", "live")["scoreboard"]["games"]
            game = next((g for g in games if g["gameId"] == self.game_id), None)
            if game is None:
                raise LookupError(f"오늘 스코어보드에 {self.game_id} 없음")
            return game
        eps = {"scoreboard": _call(sb)}
        if eps["scoreboard"]["ok"] and eps["scoreboard"]["data"]["gameStatus"] == 1:
            return {"source": self.name, "format": self.fmt, "endpoints": eps}  # 경기 전엔 박스/PBP 가 없다
        eps["boxscore"] = _call(lambda: cdn_get(f"boxscore/boxscore_{self.game_id}.json", "live")["game"])
        eps["playbyplay"] = _call(lambda: cdn_get(f"playbyplay/playbyplay_{self.game_id}.json", "live")["game"])
        raw = {"source": self.name, "format": self.fmt, "endpoints": eps}
        if not eps["scoreboard"]["ok"] and eps["boxscore"]["ok"]:
            raw["sb_note"] = f"스코어보드만 실패({eps['scoreboard']['error']}) → 박스스코어로 대신"
            eps["scoreboard"] = {"ok": True, "data": box_as_scoreboard(eps["boxscore"]["data"]), "error": None, "ms": 0}
        return raw


class StatsSource:
    """stats.nba.com 엔드포인트 (live 가 막혔을 때의 대체 경로)."""
    name, fmt = "stats.nba.com", "stats"

    def __init__(self, game_id: str):
        self.game_id = game_id
        self._game_date: str | None = None

    def _date(self) -> str:
        if self._game_date is None:
            from nba_api.stats.endpoints import scheduleleaguev2
            season = f"20{self.game_id[3:5]}-{int(self.game_id[3:5]) + 1:02d}"
            df = scheduleleaguev2.ScheduleLeagueV2(season=season, timeout=30).get_data_frames()[0]
            row = df[df["gameId"] == self.game_id].iloc[0]
            utc = datetime.fromisoformat(row["gameDateTimeUTC"].replace("Z", "+00:00"))
            self._game_date = (utc - timedelta(hours=4)).date().isoformat()  # 미 동부 기준 날짜
        return self._game_date

    def fetch(self) -> dict:
        from nba_api.stats.endpoints import boxscoretraditionalv3, playbyplayv3, scoreboardv3

        def sb():
            games = scoreboardv3.ScoreboardV3(game_date=self._date(), league_id="00",
                                              timeout=30).get_dict()["scoreboard"]["games"]
            return next(g for g in games if g["gameId"] == self.game_id)
        eps = {"scoreboard": _call(sb)}
        # 경기 전엔 박스·PBP 가 없고, 경기 중에도 stats 의 박스·PBP 는 비어 있다 (끝난 뒤에야 채워짐) → 스코어보드만
        if not eps["scoreboard"]["ok"] or eps["scoreboard"]["data"]["gameStatus"] != 3:
            return {"source": self.name, "format": self.fmt, "endpoints": eps}
        eps["boxscore"] = _call(lambda: boxscoretraditionalv3.BoxScoreTraditionalV3(
            game_id=self.game_id, timeout=30).get_dict()["boxScoreTraditional"])
        eps["playbyplay"] = _call(lambda: playbyplayv3.PlayByPlayV3(
            game_id=self.game_id, timeout=30).get_dict()["game"])
        return {"source": self.name, "format": self.fmt, "endpoints": eps}


class EspnPacer:
    """ESPN 라이브 갱신 간격: 10초 → (429·5xx·응답 없음·3초 넘는 응답) 20초 → 30초. 10분 동안 문제가 없으면 다시 10초."""

    def __init__(self):
        self.level = 0
        self.bad_at = 0.0
        self.why: str | None = None
        self.seen = 0       # 첫 요청은 연결 준비(DNS·TLS)와 서버 시작 부담이 겹쳐 느리므로 '느림' 판정에서 뺀다

    @property
    def sec(self) -> float:
        return ESPN_LIVE_SEC[self.level]

    def observe(self, er: dict) -> None:
        ep = er["endpoints"]["scoreboard"]
        err = ep.get("error") or ""
        bad = None
        if not ep["ok"]:
            m = re.search(r"HTTP (429|5\d\d)", err)
            if m:
                bad = "요청 과다(429)" if m.group(1) == "429" else f"서버 오류({m.group(1)})"
            elif "Timeout" in err or "ConnectionError" in err:
                bad = "응답 없음"
            elif "쉬는 중" in err:
                bad = "ESPN 쉬는 중"
        elif (er.get("espn_ms") or 0) > ESPN_SLOW_MS and self.seen:
            bad = f"응답 {er['espn_ms'] / 1000:.1f}초"
        self.seen += 1
        now = time.monotonic()
        if bad:
            if self.level < len(ESPN_LIVE_SEC) - 1:
                print(f"  ESPN 갱신 간격 {self.sec:g}초 → {ESPN_LIVE_SEC[self.level + 1]:g}초 ({bad})", flush=True)
            self.level = min(self.level + 1, len(ESPN_LIVE_SEC) - 1)
            self.bad_at, self.why = now, bad
        elif self.level and now - self.bad_at >= ESPN_CALM_SEC:
            print(f"  ESPN 10분 동안 정상 → 갱신 간격 {self.sec:g}초 → {ESPN_LIVE_SEC[0]:g}초", flush=True)
            self.level, self.why = 0, None


class AutoSource:
    """경기 중 출처: cdn.nba.com(정책상 열려 있고 경기가 진행 중일 때만) → ESPN 실시간 API → stats.nba.com(점수만).
    cdn 은 요청 관리자 정책(nba_http.CDN_POLICY)이 '라이브만' 으로 열려 있을 때만 시도한다 — 막혀 있으면 묻지도 않고 ESPN 으로.
    경기 전·후(진행 중이 아님)에도 cdn 을 쓰지 않는다."""

    def __init__(self, game_id: str, mode: str, game: dict | None = None):
        self.mode, self.game_id = mode, game_id
        self.live = LiveApiSource(game_id)
        self.stats = StatsSource(game_id)
        self.espn = None
        if game and game.get("home") and game.get("away") and game.get("tipoff_utc"):
            import espn_source
            self.espn = espn_source.EspnSource(game_id, game["home"], game["away"], game["tipoff_utc"])
        self.last_good = LastGood(game_id)   # 정식 라이브만: 마지막 성공 응답 저장·복원
        self.pacer = EspnPacer()
        self.used: str | None = None          # 직전 폴링에 쓴 출처: cdn | espn | stats
        self.game_status: int | None = None   # 직전에 받은 경기 상태 (1 전 · 2 진행 · 3 끝)

    def _note(self, raw: dict, used: str) -> dict:
        self.used = used
        sb = raw["endpoints"].get("scoreboard") or {}
        if sb.get("ok"):
            self.game_status = sb["data"].get("gameStatus")
        return raw

    def live_interval(self) -> tuple[float, str, str | None]:
        """경기 중 다음 폴링까지 (초, 사이트, 늘린 이유)."""
        if self.used == "cdn":
            return CDN_LIVE_SEC, NH.CDN, None
        if self.espn:
            return self.pacer.sec, NH.ESPN_HOST, self.pacer.why
        return LIVE_POLL_SEC, "stats.nba.com", None

    def fetch(self) -> dict:
        if self.mode == "stats":
            return self._note(self.stats.fetch(), "stats")
        raw = None
        if self.mode == "live" or (NH.cdn_usable_for_live() and self.game_status == 2):
            raw = self.live.fetch()
            # 박스스코어·플레이바이플레이 중 하나라도 cdn 에서 받으면 cdn 을 쓴다 (경기 전엔 스코어보드만)
            eps = raw["endpoints"]
            live_ok = eps["scoreboard"]["ok"] and (eps["scoreboard"]["data"]["gameStatus"] == 1 or
                                                   eps.get("boxscore", {}).get("ok") or eps.get("playbyplay", {}).get("ok"))
            if self.mode == "live" or live_ok:
                return self._note(raw, "cdn")
            err = raw["endpoints"]["scoreboard"]["error"] or ""
            if err.startswith("JSONDecodeError"):  # nba_api 는 403 HTML 응답을 JSON 오류로만 보여 준다
                err += " (응답이 JSON 아님 — cdn.nba.com 접근 차단(403) 추정)"
        else:
            why = NH.CDN_POLICY.refuse("live") or ("쉬는 중" if NH.CDN_POLICY.mode() == "live_only" and not NH.open_now(NH.CDN)
                                                    else "경기가 진행 중일 때만 사용")
            err = f"cdn.nba.com 사용 안 함: {why}"
        espn_err = None
        if self.espn:
            er = self.espn.fetch()
            self.pacer.observe(er)
            if er["endpoints"]["playbyplay"]["ok"] or er["endpoints"]["boxscore"]["ok"]:
                er["live_error"] = err
                NH.note_fallback(NH.CDN, "ESPN")
                return self._note(er, "espn")
            espn_err = er["endpoints"]["scoreboard"]["error"]
        fallback = self.stats.fetch()
        if raw and all(not r["ok"] for r in fallback["endpoints"].values()) and any(r["ok"] for r in raw["endpoints"].values()):
            fallback = raw   # stats 도 못 받으면 cdn 쪽 결과(일부라도)를 그대로
        fallback["live_error"] = err
        if espn_err:
            fallback["espn_error"] = espn_err
        return self._note(fallback, "stats")


class LastGood:
    """정식 라이브 응답 중 성공한 것을 엔드포인트별로 저장해 두고, 서버를 다시 켰을 때 복원한다.
    cache/live_last/<게임ID>/<형식>_<엔드포인트>.json · 리플레이(TEST)는 이 경로를 쓰지 않는다."""

    def __init__(self, game_id: str):
        self.dir = os.path.join(LIVE_LAST, game_id)
        self.hash: dict[str, str] = {}

    def save(self, raw: dict) -> None:
        os.makedirs(self.dir, exist_ok=True)
        for name, r in raw["endpoints"].items():
            if not r["ok"] or (name == "playbyplay" and not (r["data"] or {}).get("actions")):
                continue
            body = json.dumps(r["data"], sort_keys=True)
            key = f"{raw['format']}_{name}"
            h = hashlib.sha1(body.encode()).hexdigest()
            if self.hash.get(key) == h:
                continue
            tmp = os.path.join(self.dir, f".{key}.tmp")
            with open(tmp, "w") as f:
                json.dump({"saved_utc": now_utc().isoformat(timespec="seconds"), "source": raw["source"], "data": r["data"]}, f)
            os.replace(tmp, os.path.join(self.dir, f"{key}.json"))
            self.hash[key] = h

    def restore(self) -> dict | None:
        """가장 최근에 저장된 형식의 응답들 → fetch() 와 같은 모양 (+ saved: 엔드포인트별 저장 시각)."""
        best = None
        for fmt in ("live", "stats"):
            got = {}
            for name in ("scoreboard", "boxscore", "playbyplay"):
                try:
                    with open(os.path.join(self.dir, f"{fmt}_{name}.json")) as f:
                        got[name] = json.load(f)
                except (OSError, ValueError):
                    pass
            if not got:
                continue
            newest = max(v["saved_utc"] for v in got.values())
            if best is None or newest > best[0]:
                best = (newest, fmt, got)
        if not best:
            return None
        _, fmt, got = best
        eps = {n: {"ok": True, "data": v["data"], "error": None, "ms": 0} for n, v in got.items()}
        if "scoreboard" not in eps:
            if fmt != "live" or "boxscore" not in eps:
                return None
            eps["scoreboard"] = {"ok": True, "data": box_as_scoreboard(eps["boxscore"]["data"]), "error": None, "ms": 0}
        return {"source": f"저장본({next(iter(got.values()))['source']})", "format": fmt, "endpoints": eps,
                "saved": {n: v["saved_utc"] for n, v in got.items()}}


def _cached_json(path: str, get):
    """끝난 경기 데이터 캐시. 없거나 깨진 파일(예전에 받기 실패로 남은 빈 파일)이면 다시 받고, 성공했을 때만 저장한다."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    data = get()
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


class ReplaySource:
    """끝난 경기를 live 또는 stats 형식 응답처럼 흉내 낸다. 폴링·로그·화면 경로를 그대로 시험하기 위한 것."""

    def __init__(self, game_id: str, speed: float, pregame_sec: float, fmt: str = "live",
                 faults: list[tuple[str, float, float]] | None = None, real_pace: bool = False):
        self.game_id, self.speed, self.pregame, self.fmt = game_id, speed, pregame_sec, fmt
        self.faults = faults or []  # (엔드포인트, 시작 초, 끝 초) — 실행 시작부터 실제 시간
        self.name = f"replay({fmt})"
        if fmt == "live":
            pbp, box = self._load_live()
            self.actions, self.live_box = pbp["actions"], box
            self.box = {side: {"teamTricode": box[side]["teamTricode"], "players": [
                {"personId": p["personId"], "nameI": p["nameI"], "familyName": p["familyName"]}
                for p in box[side]["players"]]} for side in ("homeTeam", "awayTeam")}
        else:
            pbp, box = self._load()
            self.actions = pbp["game"]["actions"]
            self.box = box["boxScoreTraditional"]
        self.start = time.monotonic()
        # 재생 위치: 음수 = 팁오프 전 대기(실제 초), 0 이상 = 경기 경과 시간(경기 초)
        self.pos = -float(pregame_sec)
        self.playing = True
        self.seek_gen = 0   # 슬라이더로 이동할 때마다 1씩 — 이동 전 위치로 계산한 화면과 구분한다
        self._wall = time.monotonic()
        self._clock_lock = threading.Lock()
        self.tipoff = (now_utc() + timedelta(seconds=pregame_sec)).isoformat()
        self.total = elapsed(self.actions[-1]["period"], clock_sec(self.actions[-1]["clock"])) if self.actions else 0
        # 시험용 '실제 시간' 재생: 플레이의 실제 발생 시각(timeActual)대로 흘러 파울·타임아웃·쿼터 휴식에 시계가 멈춘다.
        # 이때 재생 위치(pos·total)는 경기 시간이 아니라 팁오프 뒤 실제 초다.
        self.wall_t: list[float] | None = None
        if real_pace and self.actions and all(a.get("timeActual") for a in self.actions):
            t0 = _parse_actual(self.actions[0]["timeActual"])
            self.wall_t, top = [], 0.0
            for a in self.actions:
                top = max(top, (_parse_actual(a["timeActual"]) - t0).total_seconds())   # 순서가 뒤집힌 기록은 앞 시각에 맞춤
                self.wall_t.append(top)
            self.game_t = [elapsed(a["period"], clock_sec(a["clock"])) for a in self.actions]
            self.total = self.wall_t[-1]
            self.name = f"replay({fmt}, 실제 시간)"
        self.roster = {}
        for side in ("homeTeam", "awayTeam"):
            t = self.box[side]
            self.roster[t["teamTricode"]] = t["players"]

    def _load(self) -> tuple[dict, dict]:
        from nba_api.stats.endpoints import boxscoretraditionalv3, playbyplayv3
        d = os.path.join(REPLAY_DATA, self.game_id)
        os.makedirs(d, exist_ok=True)
        files = {"pbp_v3.json": lambda: playbyplayv3.PlayByPlayV3(game_id=self.game_id, timeout=60).get_dict(),
                 "box_v3.json": lambda: boxscoretraditionalv3.BoxScoreTraditionalV3(
                     game_id=self.game_id, timeout=60).get_dict()}
        out = [_cached_json(os.path.join(d, name), get) for name, get in files.items()]
        return out[0], out[1]

    def _load_live(self) -> tuple[dict, dict]:
        from nba_api.live.nba.endpoints import boxscore, playbyplay
        d = os.path.join(REPLAY_DATA, self.game_id)
        os.makedirs(d, exist_ok=True)
        files = {"live_playbyplay.json": lambda: playbyplay.PlayByPlay(
                     self.game_id, headers=BROWSER_HEADERS, timeout=30).get_dict()["game"],
                 "live_boxscore.json": lambda: boxscore.BoxScore(
                     self.game_id, headers=BROWSER_HEADERS, timeout=30).get_dict()["game"]}
        try:
            out = [_cached_json(os.path.join(d, name), get) for name, get in files.items()]
            self.data_source = "NBA.com"
            return out[0], out[1]
        except Exception as exc:
            # NBA(cdn) 가 막혔으면 ESPN 경기 요약으로 재생한다 (두 파일을 같은 출처로 맞춘다: 선수 ID 가 섞이지 않게)
            print(f"  ! 리플레이 데이터: NBA 에서 받지 못함 ({type(exc).__name__}) → ESPN 경기 요약으로", flush=True)
            import espn_source
            pbp = _cached_json(os.path.join(d, "espn_playbyplay.json"),
                               lambda: espn_source.final_game(self.game_id)["pbp"])
            box = _cached_json(os.path.join(d, "espn_boxscore.json"),
                               lambda: espn_source.final_game(self.game_id)["box"])
            self.data_source = "ESPN"
            NH.note_fallback("cdn.nba.com", "ESPN")
            return pbp, box

    def fetch(self) -> dict:
        gen = self.seek_gen          # 위치를 읽기 전에 먼저 읽는다 (이동과 겹치면 다음 계산에서 한 번 더 새로 계산)
        raw = self._fetch()
        raw["seek_gen"] = gen
        elapsed_real = time.monotonic() - self.start
        for name, a, b in self.faults:
            if a <= elapsed_real < b and name in raw["endpoints"]:
                raw["endpoints"][name] = {"ok": False, "data": None, "ms": 0,
                                          "error": "테스트용으로 일부러 실패시킨 응답"}
        return raw

    # ---- 재생 제어 (웹 화면의 버튼에서 호출)
    def _advance(self) -> float:
        with self._clock_lock:
            now = time.monotonic()
            dt, self._wall = now - self._wall, now
            if self.playing and dt > 0:
                if self.pos < 0:                      # 대기 시간은 배속과 무관하게 실제 시간으로 흐른다
                    used = min(dt, -self.pos)
                    self.pos += used
                    dt -= used
                if self.pos >= 0:
                    self.pos = min(self.pos + dt * self.speed, self.total)
            if self.pos < 0:  # 일시정지하면 팁오프 예정 시각도 그만큼 미뤄진다
                self.tipoff = (now_utc() + timedelta(seconds=-self.pos)).isoformat()
            return self.pos

    def control(self, action: str, value=None) -> dict:
        self._advance()
        with self._clock_lock:
            if action == "speed":
                self.speed = max(0.25, min(float(value), 120))
            elif action == "pause":
                self.playing = False
            elif action == "play":
                self.playing = True
            elif action == "seek":            # 타임라인 슬라이더: 경기 경과 시간(초)으로 바로 이동
                self.pos = max(0.0, min(float(value), self.total))
                self.seek_gen += 1
        return self.replay_info()

    def replay_info(self) -> dict:
        self._advance()
        with self._clock_lock:
            return {"speed": self.speed, "playing": self.playing, "pos": round(self.pos, 1), "total": self.total,
                    "data_source": getattr(self, "data_source", "NBA.com")}

    def poll_interval(self, base: float) -> float:
        """실제 경기의 30초 간격을 배속에 맞춰 줄인다 (0.5~3초)."""
        return max(0.5, min(base, 30 / self.speed))

    def _game_at(self, w: float) -> float:
        """실제 시간 재생: 팁오프 뒤 실제 w초의 경기 경과 시간. 두 플레이 사이에서 시계는 뒤 플레이에 닿도록 끝부분에서만
        흐른다고 본다 (앞 플레이 직후의 정지 — 파울·자유투·타임아웃·쿼터 휴식 — 를 재현)."""
        import bisect
        i = bisect.bisect_right(self.wall_t, w) - 1
        if i < 0:
            return 0.0
        g = max(self.game_t[:i + 1])
        if i + 1 >= len(self.wall_t):
            return g
        nxt = self.game_t[i + 1]
        return max(g, min(nxt, nxt - (self.wall_t[i + 1] - w)))

    def _fetch(self) -> dict:
        sim = self._advance()
        home, away = self.box["homeTeam"], self.box["awayTeam"]
        base = {"gameId": self.game_id, "gameTimeUTC": self.tipoff,
                "homeTeam": {"teamTricode": home["teamTricode"], "score": 0},
                "awayTeam": {"teamTricode": away["teamTricode"], "score": 0}}
        if sim < 0:
            sb = {**base, "gameStatus": 1, "gameStatusText": "경기 전", "period": 0, "gameClock": ""}
            return {"source": self.name, "format": self.fmt, "endpoints": {"scoreboard": _ok(sb)}}

        visible = []
        if self.wall_t is not None:
            visible = [a for a, w in zip(self.actions, self.wall_t) if w <= sim]
            sim = self._game_at(sim)
        for a in ([] if self.wall_t is not None else self.actions):
            if elapsed(a["period"], clock_sec(a["clock"])) > sim:
                break
            visible.append(a)
        done = len(visible) == len(self.actions)
        last = visible[-1] if visible else None
        sh = sa = 0
        for a in visible:
            if to_int(a["scoreHome"]) is not None:
                sh, sa = int(a["scoreHome"]), int(a["scoreAway"])
        period = last["period"] if last else 1
        sb = {**base, "gameStatus": 3 if done else 2, "gameStatusText": "Final" if done else "Live",
              "period": period, "gameClock": "PT00M00.00S" if done else _iso(_remaining(sim, period))}
        sb["homeTeam"]["score"], sb["awayTeam"]["score"] = sh, sa
        box = self._live_box_until(visible) if self.fmt == "live" else self._box_until(visible)
        return {"source": self.name, "format": self.fmt, "endpoints": {
            "scoreboard": _ok(sb),
            "boxscore": _ok(box),
            "playbyplay": _ok({"gameId": self.game_id, "actions": visible}),
        }}

    def _live_box_until(self, actions: list[dict]) -> dict:
        stats: dict[int, dict] = {}
        seen = set()
        tris = [self.live_box[s]["teamTricode"] for s in ("homeTeam", "awayTeam")]
        teams = {tri: _empty_team() for tri in tris}
        lineup = {self.live_box[s]["teamTricode"]: {p["personId"] for p in self.live_box[s]["players"]
                                                    if p.get("starter") == "1"} for s in ("homeTeam", "awayTeam")}
        foul_lineup, last_score = None, (0, 0)
        on_since = {pid: 0.0 for ids in lineup.values() for pid in ids} if actions else {}
        seconds: dict[int, float] = {}

        def bump(pid, key, n=1):
            if pid:
                s = stats.setdefault(pid, _empty_player())
                s[key] += n
        for a in actions:
            seen.add(a.get("personId"))
            kind, made = a["actionType"], a.get("shotResult") == "Made"
            tri = a.get("teamTricode")
            _team_count(teams.get(tri), kind, made, value=3 if kind == "3pt" else 2,
                        assisted=bool(a.get("assistPersonId")), fmt="live")
            if a.get("personId") and kind != "substitution":
                _player_count(stats.setdefault(a["personId"], _empty_player()), kind, made,
                              value=3 if kind == "3pt" else 2, fmt="live",
                              foul_personal=a.get("subType") != "technical")
            if kind == "substitution" and tri in lineup:
                pid, t_now = a["personId"], elapsed(a["period"], clock_sec(a["clock"]))
                if a.get("subType") == "in":
                    lineup[tri].add(pid)
                    on_since.setdefault(pid, t_now)
                else:
                    lineup[tri].discard(pid)
                    if pid in on_since:
                        seconds[pid] = seconds.get(pid, 0) + t_now - on_since.pop(pid)
            elif kind == "foul":  # 자유투 +-는 파울 순간 코트 위 선수 기준 (NBA 공식 방식)
                foul_lineup = (a["period"], a["clock"], {k: set(v) for k, v in lineup.items()})
            h, w = to_int(a.get("scoreHome")) or 0, to_int(a.get("scoreAway")) or 0
            if (h, w) != last_score:
                on = lineup
                if kind == "freethrow" and foul_lineup and foul_lineup[:2] == (a["period"], a["clock"]):
                    on = foul_lineup[2]
                for side_tri, delta in ((tris[0], (h - last_score[0]) - (w - last_score[1])),
                                        (tris[1], (w - last_score[1]) - (h - last_score[0]))):
                    for pid in on[side_tri]:
                        bump(pid, "plusMinusPoints", delta)
                last_score = (h, w)
            if kind in ("2pt", "3pt") and made:
                bump(a["personId"], "points", 2 if kind == "2pt" else 3)
                bump(a.get("assistPersonId"), "assists")
            elif kind == "freethrow" and made:
                bump(a["personId"], "points")
            elif kind == "rebound" and "team" not in (a.get("qualifiers") or []):
                bump(a["personId"], "reboundsTotal")
        now_t = elapsed(actions[-1]["period"], clock_sec(actions[-1]["clock"])) if actions else 0
        for pid, since in on_since.items():
            seconds[pid] = seconds.get(pid, 0) + now_t - since
        on_court = set().union(*lineup.values()) if lineup else set()
        out = {}
        for side in ("homeTeam", "awayTeam"):
            t = self.live_box[side]
            out[side] = {"teamTricode": t["teamTricode"], "statistics": teams[t["teamTricode"]], "players": [
                {"personId": p["personId"], "nameI": p["nameI"], "starter": p.get("starter", "0"),
                 "oncourt": "1" if p["personId"] in on_court and actions else "0",
                 "played": "1" if p["personId"] in seen or p.get("starter") == "1" and actions else "0",
                 "statistics": {**stats.get(p["personId"], _empty_player()),
                                "minutes": _iso(seconds.get(p["personId"], 0))}}
                for p in t["players"]]}
        return out

    def _box_until(self, actions: list[dict]) -> dict:
        stats: dict[int, dict] = {}
        name_to_id = {tri: {p["familyName"]: p["personId"] for p in ps} for tri, ps in self.roster.items()}

        def bump(pid, key, n=1):
            if pid:
                s = stats.setdefault(pid, _empty_player())
                s[key] += n
        teams = {tri: _empty_team() for tri in self.roster}
        team_ids = set()
        for side in ("homeTeam", "awayTeam"):
            team_ids.add(self.box[side].get("teamId"))
        for a in actions:
            pid, kind, desc = a["personId"], a["actionType"], a["description"] or ""
            _team_count(teams.get(a["teamTricode"]), kind, made=kind == "Made Shot" or (
                kind == "Free Throw" and not desc.startswith("MISS")), value=a.get("shotValue"),
                assisted=bool(_AST.search(desc)), fmt="stats")
            if pid and pid not in team_ids:
                sub = "STEAL" if kind == "" and "STEAL" in desc else "BLOCK" if kind == "" and "BLOCK" in desc else kind
                _player_count(stats.setdefault(pid, _empty_player()), sub,
                              made=kind == "Made Shot" or (kind == "Free Throw" and not desc.startswith("MISS")),
                              value=a.get("shotValue") or 0, fmt="stats",
                              foul_personal=not any(k in (a.get("subType") or "") for k in ("Technical", "Defense 3 Second")))
            if kind == "Made Shot":
                bump(pid, "points", a["shotValue"])
                m = _AST.search(desc)
                if m:
                    bump(name_to_id.get(a["teamTricode"], {}).get(m.group(1)), "assists")
            elif kind == "Free Throw" and not desc.startswith("MISS"):
                bump(pid, "points")
            elif kind == "Rebound" and "REBOUND (Off:" in desc:
                bump(pid, "reboundsTotal")
        out = {}
        for side in ("homeTeam", "awayTeam"):
            t = self.box[side]
            out[side] = {"teamTricode": t["teamTricode"], "statistics": teams[t["teamTricode"]], "players": [
                {**{k: p[k] for k in ("personId", "nameI", "familyName", "position")},
                 "statistics": {**stats.get(p["personId"], _empty_player()), "minutes": "",
                                "plusMinusPoints": None}}
                for p in t["players"]]}
        return out


PLAYER_STAT_KEYS = ["points", "reboundsTotal", "assists", "plusMinusPoints", "fieldGoalsMade",
                    "fieldGoalsAttempted", "threePointersMade", "threePointersAttempted", "freeThrowsMade",
                    "freeThrowsAttempted", "steals", "blocks", "turnovers", "foulsPersonal"]


def _empty_player() -> dict:
    return dict.fromkeys(PLAYER_STAT_KEYS, 0)


def _player_count(s: dict, kind: str, made: bool, value: int, fmt: str, foul_personal: bool) -> None:
    """리플레이용: 플레이 하나를 해당 선수 기록에 반영 (득점·리바운드·어시스트 제외)."""
    shot = kind in ("2pt", "3pt") if fmt == "live" else kind in ("Made Shot", "Missed Shot")
    ft = kind == ("freethrow" if fmt == "live" else "Free Throw")
    if shot:
        s["fieldGoalsAttempted"] += 1
        s["threePointersAttempted"] += value == 3
        s["fieldGoalsMade"] += made
        s["threePointersMade"] += made and value == 3
    elif ft:
        s["freeThrowsAttempted"] += 1
        s["freeThrowsMade"] += made
    elif kind in ("steal", "STEAL"):
        s["steals"] += 1
    elif kind in ("block", "BLOCK"):
        s["blocks"] += 1
    elif kind in ("turnover", "Turnover"):
        s["turnovers"] += 1
    elif kind in ("foul", "Foul") and foul_personal:
        s["foulsPersonal"] += 1


def _empty_team() -> dict:
    return {"points": 0, "fieldGoalsMade": 0, "fieldGoalsAttempted": 0, "threePointersMade": 0,
            "threePointersAttempted": 0, "freeThrowsMade": 0, "freeThrowsAttempted": 0,
            "turnoversTotal": 0, "assists": 0}


def _team_count(t: dict | None, kind: str, made: bool, value, assisted: bool, fmt: str) -> None:
    """리플레이용: 플레이 하나를 팀 기록에 반영."""
    if t is None:
        return
    shot = kind in ("2pt", "3pt") if fmt == "live" else kind in ("Made Shot", "Missed Shot")
    ft = kind == ("freethrow" if fmt == "live" else "Free Throw")
    tov = kind == ("turnover" if fmt == "live" else "Turnover")
    if shot:
        t["fieldGoalsAttempted"] += 1
        t["threePointersAttempted"] += value == 3
        if made:
            t["fieldGoalsMade"] += 1
            t["threePointersMade"] += value == 3
            t["points"] += value
            t["assists"] += assisted
    elif ft:
        t["freeThrowsAttempted"] += 1
        if made:
            t["freeThrowsMade"] += 1
            t["points"] += 1
    elif tov:
        t["turnoversTotal"] += 1


def _parse_actual(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None, "ms": 0}


def _remaining(sim: float, period: int) -> float:
    start = (period - 1) * 720 if period <= 4 else 2880 + (period - 5) * 300
    length = 720 if period <= 4 else 300
    return max(start + length - sim, 0)


def _iso(sec: float) -> str:
    return f"PT{int(sec) // 60:02d}M{sec % 60:05.2f}S"


# ---------------------------------------------------------------- 정규화 → 화면 상태

# 받기에 실패하면 직전 값을 그대로 유지할 화면 영역
CARRY_OVER = {"playbyplay": ("margin", "plays", "now_t", "alerts", "win_prob", "shots", "rotation"),
              "boxscore": ("players", "teams", "rotation")}

# 승리 확률: 데이터 소스(nba_api live / stats)에 값이 없어 점수 차와 남은 시간으로 계산한다.
# 남은 시간 동안의 점수 차 변화를 정규분포로 보는 단순 모델 (선수 구성·공격권·파울 상황은 반영 안 함)
WP_SIGMA = 13.0        # 경기 전체 최종 점수 차의 표준편차 (NBA 대략값)
WP_HOME_EDGE = 2.5     # 홈 어드밴티지(점), 남은 시간 비율만큼 반영
WP_FLOOR = 1.5         # 시간이 남아 있으면 마지막 공격 한 번의 불확실성은 남는다
WP_STEP = 10           # 그래프 샘플 간격(경기 초)


def shot_list(acts: list[dict]) -> list[dict]:
    """히트맵용 야투 위치. 좌표는 림 중심 기준 0.1피트 (live·stats 형식 모두 xLegacy/yLegacy)."""
    out = []
    for a in acts:
        kind = a.get("actionType")
        if kind in ("2pt", "3pt"):
            made, value = a.get("shotResult") == "Made", 3 if kind == "3pt" else 2
        elif kind in ("Made Shot", "Missed Shot"):
            made, value = kind == "Made Shot", a.get("shotValue") or 2
        else:
            continue
        x, y = a.get("xLegacy"), a.get("yLegacy")
        if x is None or y is None:
            continue
        out.append({"team": a.get("teamTricode"), "x": x, "y": y, "made": made, "v": value,
                    "t": elapsed(a["period"], clock_sec(a["clock"])),
                    "who": a.get("playerNameI") or a.get("playerName") or ""})
    return out


def home_win_prob(t: float, home_margin: int) -> float:
    import math
    remaining = 2880 - t if t <= 2880 else (300 - ((t - 2880) % 300)) % 300
    frac = max(remaining, 0) / 2880
    if frac <= 0:
        return 1.0 if home_margin > 0 else 0.0 if home_margin < 0 else 0.5
    z = (home_margin + WP_HOME_EDGE * frac) / (WP_SIGMA * math.sqrt(frac) + WP_FLOOR)
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def win_prob_series(acts: list[dict], now_t: float, final: bool) -> dict:
    scores = []  # (t, home, away) 점수가 바뀐 시점
    last = (0, 0)
    for a in acts:
        h, w = to_int(a.get("scoreHome")), to_int(a.get("scoreAway"))
        if h is None or w is None or (h, w) == last:
            continue
        last = (h, w)
        scores.append((elapsed(a["period"], clock_sec(a["clock"])), h, w))
    times = sorted({*range(0, int(now_t) + 1, WP_STEP), now_t, *(t for t, _, _ in scores)})
    series, i, h, w = [], 0, 0, 0
    for t in times:
        while i < len(scores) and scores[i][0] <= t:
            _, h, w = scores[i]
            i += 1
        p = home_win_prob(t, h - w)
        series.append({"t": t, "p": round(p, 4), "h": h, "a": w})
    if final and series:
        series[-1]["p"] = 1.0 if h > w else 0.0 if h < w else 0.5
    return {"series": series, "current": series[-1]["p"] if series else None,
            "model": {"sigma": WP_SIGMA, "home_edge": WP_HOME_EDGE, "source": "점수 차·남은 시간 모델"}}


def normalize(raw: dict, prev: dict | None) -> dict:
    eps = raw["endpoints"]
    # 경기 중에 '성공'인데 플레이가 하나도 없으면(stats 대체 경로에서 흔함) 실패로 보고 직전 내용을 유지한다
    pb = eps.get("playbyplay")
    if pb and pb["ok"] and not (pb["data"] or {}).get("actions") and prev and prev.get("plays"):
        eps["playbyplay"] = {"ok": False, "data": None, "error": "플레이 기록이 비어 있음", "ms": pb["ms"]}
    at = now_utc().isoformat(timespec="milliseconds")
    last_ok = dict((prev or {}).get("last_ok", {}))
    sb = eps["scoreboard"]["data"] if eps["scoreboard"]["ok"] else None
    if sb is None:
        state = dict(prev) if prev else {"status": "unknown", "message": "아직 데이터를 받지 못했습니다."}
        failed = [n for n, r in eps.items() if not r["ok"]]
        return _mark_freshness(state, eps, failed, last_ok)

    home, away = sb["homeTeam"]["teamTricode"], sb["awayTeam"]["teamTricode"]
    status = {1: "waiting", 2: "live", 3: "final"}.get(sb["gameStatus"], "unknown")
    tip = sb.get("gameTimeUTC")
    state = {
        "status": status, "status_text": sb.get("gameStatusText", ""),
        "home": home, "away": away, "focus": FOCUS if FOCUS in (home, away) else home,
        "score": {home: to_int(sb["homeTeam"].get("score")) or 0, away: to_int(sb["awayTeam"].get("score")) or 0},
        "period": sb.get("period") or 0, "clock": fmt_clock(clock_sec(sb.get("gameClock"))),
        "tipoff_utc": tip, "margin": [], "plays": [], "players": [], "teams": {}, "alerts": [],
        "error": None,
    }
    state["opponent"] = away if state["focus"] == home else home
    _freshest_score(state, eps, raw["format"], home, away)
    state["period_label"] = period_label(state["period"]) if state["period"] else ""

    pbp = eps.get("playbyplay", {})
    if pbp.get("ok"):
        acts = pbp["data"]["actions"]
        sign = 1 if state["focus"] == home else -1
        last_m, last_score, scored = 0, (0, 0), set()
        series = [{"t": 0, "m": 0}]
        for a in acts:
            h, w = to_int(a.get("scoreHome")), to_int(a.get("scoreAway"))
            if h is None or w is None:
                continue
            if (h, w) != last_score:
                scored.add(id(a))
                last_score = (h, w)
            m = (h - w) * sign
            if m != last_m:
                series.append({"t": elapsed(a["period"], clock_sec(a["clock"])), "m": m, "h": h, "a": w,
                               "label": f"{period_label(a['period'])} {fmt_clock(clock_sec(a['clock']))}"})
                last_m = m
        state["margin"] = series
        recent = [a for a in acts if a.get("description")][-RECENT_PLAYS:][::-1]
        state["plays"] = [{"label": f"{period_label(a['period'])} {fmt_clock(clock_sec(a['clock']))}",
                           "team": a.get("teamTricode") or "", "desc": a["description"],
                           "score": f"{a['scoreAway']}-{a['scoreHome']}" if id(a) in scored else ""}
                          for a in recent]
        if acts and state["status"] != "waiting":
            last = acts[-1]
            state["now_t"] = elapsed(last["period"], clock_sec(last["clock"]))
        state["alerts"] = game_alerts(acts, home, away, state["focus"])
        state["shots"] = shot_list(acts)

    box = eps.get("boxscore", {})
    failed = [n for n, r in eps.items() if not r["ok"]]
    for name in failed:
        for key in CARRY_OVER.get(name, ()):
            if prev and key in prev:
                state[key] = prev[key]
    for name, r in eps.items():
        if r["ok"]:
            last_ok[name] = at
    if box.get("ok"):
        for side in ("awayTeam", "homeTeam"):
            t = box["data"][side]
            for p in t["players"]:
                st = p.get("statistics", {})
                if raw["format"] == "live" and p.get("played") == "0":
                    continue
                pts, reb, ast = st.get("points", 0), st.get("reboundsTotal", 0), st.get("assists", 0)
                if raw["format"] == "stats" and not (pts or reb or ast or st.get("minutes")):
                    continue
                sec = _minutes_sec(st.get("minutes"))
                starter = p.get("starter") == "1" if "starter" in p else bool(p.get("position"))
                state["players"].append({
                    "id": p.get("personId"), "team": t["teamTricode"], "name": p.get("nameI") or p.get("name"),
                    "starter": starter, "oncourt": p.get("oncourt") == "1",
                    "sec": sec, "min": None if sec is None else f"{int(sec) // 60}:{int(sec) % 60:02d}",
                    "pts": pts, "reb": reb, "ast": ast, "pm": to_int(st.get("plusMinusPoints")),
                    **{k: st.get(src, 0) or 0 for k, src in (
                        ("fgm", "fieldGoalsMade"), ("fga", "fieldGoalsAttempted"),
                        ("fg3m", "threePointersMade"), ("fg3a", "threePointersAttempted"),
                        ("ftm", "freeThrowsMade"), ("fta", "freeThrowsAttempted"), ("stl", "steals"),
                        ("blk", "blocks"), ("tov", "turnovers"), ("pf", "foulsPersonal"))}})
            if t.get("statistics"):
                team_reb = sum(r["reb"] for r in state["players"] if r["team"] == t["teamTricode"])
                state["teams"][t["teamTricode"]] = team_stats(t["statistics"], team_reb)
        state["players"].sort(key=lambda r: (r["team"] != state["focus"], not r["starter"],
                                             -(r["sec"] or 0), -r["pts"]))
    # 승리 확률: 플레이 기록 기준 + 가장 최신 점수·시계 (플레이 기록이 비거나 늦어도 지금 상황을 반영)
    if state["status"] in ("live", "final") and state.get("period"):
        cur_t = elapsed(state["period"], clock_sec_label(state["clock"]))
        state["now_t"] = max(state.get("now_t", 0), cur_t)
        if pbp.get("ok"):
            wp = win_prob_series(pbp["data"]["actions"], state["now_t"], state["status"] == "final")
        elif prev and prev.get("win_prob"):
            wp = {**prev["win_prob"], "series": list(prev["win_prob"]["series"])}   # 지난 흐름은 그대로 두고
        else:
            wp = {"series": [], "current": None, "model": {"sigma": WP_SIGMA, "home_edge": WP_HOME_EDGE}}
        if True:
            h, w = state["score"][home], state["score"][away]
            last = wp["series"][-1] if wp["series"] else None
            if not last or last["t"] < state["now_t"] or (last["h"], last["a"]) != (h, w):
                p_now = home_win_prob(state["now_t"], h - w)
                if state["status"] == "final":
                    p_now = 1.0 if h > w else 0.0 if h < w else 0.5
                wp["series"].append({"t": state["now_t"], "p": round(p_now, 4), "h": h, "a": w})
                wp["current"] = wp["series"][-1]["p"]
            state["win_prob"] = wp
    # 5) 하프타임·쿼터 휴식
    state["break"] = None
    if state["status"] == "live" and state.get("period") and clock_sec_label(state["clock"]) == 0:
        state["break"] = "하프타임" if state["period"] == 2 else "쿼터 휴식" if state["period"] in (1, 3) else "연장 전 휴식"
    if pbp.get("ok") and box.get("ok"):
        state["rotation"] = rotation(pbp["data"]["actions"], box["data"], raw["format"], home, away,
                                     state.get("now_t", 0))
        for w in state["rotation"]["warnings"]:
            _warn_once(w)
    return _mark_freshness(state, eps, failed, last_ok)


_WARNED: set[str] = set()


def assist_network(raw: dict, state: dict) -> dict | None:
    """그 시점까지의 어시스트 연결: 패스한 선수 → 득점한 선수 (팀별). live 형식 문자중계만 지원."""
    pb = (raw.get("endpoints") or {}).get("playbyplay") or {}
    acts = ((pb.get("data") or {}) if pb.get("ok") else {}).get("actions")
    if acts is None or raw.get("format") != "live":
        return None
    names = {p["id"]: p["name"] for p in state.get("players", []) if p.get("id")}
    out = {}
    for a in acts:
        aid, sid = a.get("assistPersonId"), a.get("personId")
        if not aid or a.get("shotResult") != "Made" or a.get("actionType") not in ("2pt", "3pt"):
            continue
        t = out.setdefault(a.get("teamTricode"), {"edges": {}, "nodes": {}})
        e = t["edges"].setdefault(f"{aid}>{sid}", {"from": aid, "to": sid, "n": 0, "pts": 0})
        e["n"] += 1
        e["pts"] += 3 if a["actionType"] == "3pt" else 2
        for pid, nm, k in ((aid, a.get("assistPlayerNameInitial"), "ast"), (sid, a.get("playerNameI"), "made")):
            nd = t["nodes"].setdefault(pid, {"id": pid, "name": names.get(pid) or nm or str(pid), "ast": 0, "made": 0})
            nd[k] += 1
    return {tri: {"edges": sorted(t["edges"].values(), key=lambda e: -e["n"]),
                  "nodes": sorted(t["nodes"].values(), key=lambda n: -(n["ast"] + n["made"]))}
            for tri, t in out.items() if tri}


def replay_marks(actions: list[dict], home: str, away: str) -> dict:
    """하이라이트 표시용 (경기 전체 기준 — 화면에서 토글을 켰을 때만 요청한다):
    역전(리드가 바뀐 순간), 8점 이상 연속 득점(런), 두 팀 각각 최대 리드 순간."""
    marks, last_lead, run, best = [], 0, {"team": None, "pts": 0, "start": 0.0, "opp": 0}, {home: (0, 0.0), away: (0, 0.0)}
    sh = sa = 0
    for a in actions:
        if to_int(a.get("scoreHome")) is None:
            continue
        nh, na = int(a["scoreHome"]), int(a["scoreAway"])
        if (nh, na) == (sh, sa):
            continue
        t = elapsed(a["period"], clock_sec(a["clock"]))
        scorer = home if nh > sh else away
        pts = (nh - sh) + (na - sa)
        if scorer == run["team"]:
            run["pts"] += pts
        else:
            if run["pts"] >= 8:
                marks.append({"kind": "run", "t": run["end"], "team": run["team"], "label": f"{run['team']} {run['pts']}-0 런"})
            run = {"team": scorer, "pts": pts, "start": t}
        run["end"] = t
        sh, sa = nh, na
        d = sh - sa
        lead = (d > 0) - (d < 0)
        if lead and last_lead and lead != last_lead:
            marks.append({"kind": "flip", "t": t, "team": home if lead > 0 else away, "label": f"역전 · {home if lead > 0 else away} 앞섬"})
        if lead:
            last_lead = lead
        for tri, m in ((home, d), (away, -d)):
            if m > best[tri][0]:
                best[tri] = (m, t)
    if run["pts"] >= 8:
        marks.append({"kind": "run", "t": run["end"], "team": run["team"], "label": f"{run['team']} {run['pts']}-0 런"})
    for tri, (m, t) in best.items():
        if m > 0:
            marks.append({"kind": "max", "t": t, "team": tri, "label": f"{tri} 최대 {m}점 차 리드"})
    return {"marks": sorted(marks, key=lambda m: m["t"])}


def _warn_once(msg: str) -> None:
    if msg not in _WARNED:
        _WARNED.add(msg)
        print(f"  ⚠ 라인업 경고: {msg}", flush=True)


def rotation(acts: list[dict], box: dict, fmt: str, home: str, away: str, now_t: float) -> dict:
    """선발(박스스코어) + 교체(플레이바이플레이)로 선수별 출전 구간(stint)과 온오프 집계를 만든다.

    - 같은 시각의 이벤트를 다 처리한 뒤마다, 그리고 쿼터 경계에서 팀당 코트 위 5명인지 검사한다.
    - 자유투 득실은 파울 순간 코트에 있던 선수에게 준다 (NBA 공식 +/- 와 같은 방식).
    """
    teams = (home, away)
    info: dict[int, dict] = {}
    by_family: dict[str, dict[str, int]] = {t: {} for t in teams}
    lineup: dict[str, set[int]] = {t: set() for t in teams}
    for side in ("homeTeam", "awayTeam"):
        t = box[side]
        tri = t["teamTricode"]
        for p in t["players"]:
            starter = p.get("starter") == "1" if "starter" in p else bool(p.get("position"))
            info[p["personId"]] = {"name": p.get("nameI") or p.get("name"), "team": tri, "starter": starter}
            by_family.setdefault(tri, {})[p.get("familyName", "")] = p["personId"]
            if starter:
                lineup[tri].add(p["personId"])
    warnings: list[str] = []
    for tri in teams:
        if len(lineup[tri]) != 5:
            warnings.append(f"{tri} 선발이 {len(lineup[tri])}명으로 기록됨 (박스스코어)")

    stints: dict[int, list[dict]] = {}
    open_st: dict[int, dict] = {}
    # 온오프 누적: 선수별 [코트에 있을 때] 팀 기준 득점·실점·포세션 재료
    on = {pid: {"pf": 0, "pa": 0, "fga": 0, "oreb": 0, "tov": 0, "fta": 0,
                "o_fga": 0, "o_oreb": 0, "o_tov": 0, "o_fta": 0} for pid in info}
    tot = {tri: {"pts": 0, "fga": 0, "oreb": 0, "tov": 0, "fta": 0} for tri in teams}

    def start(pid, t):
        if pid in info and pid not in open_st:
            open_st[pid] = {"s": t, "e": t, "pf": 0, "pa": 0}

    def stop(pid, t):
        st = open_st.pop(pid, None)
        if st:
            st["e"] = t
            stints.setdefault(pid, []).append(st)

    if acts:
        for tri in teams:
            for pid in lineup[tri]:
                start(pid, 0.0)
    last_score = (0, 0)
    last_miss: str | None = None
    foul_lineup = None
    cur_key = None

    def check(label: str) -> None:
        for tri in teams:
            if len(lineup[tri]) != 5:
                warnings.append(f"{label}: {tri} 코트 위 {len(lineup[tri])}명")

    for i, a in enumerate(acts):
        period, clock = a["period"], clock_sec(a["clock"])
        t = elapsed(period, clock)
        key = (period, clock)
        if cur_key is not None and key != cur_key:
            check(f"{period_label(cur_key[0])} {fmt_clock(cur_key[1])}")
        cur_key = key
        kind, tri = a.get("actionType"), a.get("teamTricode")
        if kind == "period" and a.get("subType") == "start" and period > 1:
            check(f"{period_label(period)} 시작")
        # 교체
        if kind == "substitution" and tri in lineup:          # live: in/out 이 각각 이벤트
            pid = a.get("personId")
            if a.get("subType") == "out":
                lineup[tri].discard(pid)
                stop(pid, t)
            else:
                lineup[tri].add(pid)
                start(pid, t)
            continue
        if kind == "Substitution" and tri in lineup:          # stats: "SUB: 들어온선수 FOR 나간선수"
            m = re.match(r"SUB: (.+?) FOR (.+)$", a.get("description") or "")
            out_id = a.get("personId")
            in_id = by_family.get(tri, {}).get(m.group(1).strip()) if m else None
            lineup[tri].discard(out_id)
            stop(out_id, t)
            if in_id:
                lineup[tri].add(in_id)
                start(in_id, t)
            continue
        if kind in ("foul", "Foul"):
            foul_lineup = (key, {k: set(v) for k, v in lineup.items()})
        # 포세션 재료
        made = a.get("shotResult") == "Made" or kind == "Made Shot"
        if kind in ("2pt", "3pt", "Made Shot", "Missed Shot") and tri in tot:
            _tally(tot, on, lineup, tri, teams, "fga")
            if not made:
                last_miss = tri
        elif kind in ("freethrow", "Free Throw") and tri in tot:
            _tally(tot, on, lineup, tri, teams, "fta")
            if not (a.get("shotResult") == "Made" or not (a.get("description") or "").startswith("MISS")):
                last_miss = tri
        elif kind in ("rebound", "Rebound") and tri in tot:
            off = a.get("subType") == "offensive" if fmt == "live" else tri == last_miss
            if off:
                _tally(tot, on, lineup, tri, teams, "oreb")
        elif kind in ("turnover", "Turnover") and tri in tot:
            _tally(tot, on, lineup, tri, teams, "tov")
        # 득점 → 코트 위 선수 구간과 온오프
        h, w = to_int(a.get("scoreHome")), to_int(a.get("scoreAway"))
        if h is None or w is None or (h, w) == last_score:
            continue
        dh, dw = h - last_score[0], w - last_score[1]
        last_score = (h, w)
        cur = lineup
        if kind in ("freethrow", "Free Throw") and foul_lineup and foul_lineup[0] == key:
            cur = foul_lineup[1]
        tot[home]["pts"] += dh
        tot[away]["pts"] += dw
        for side_tri, pf, pa in ((home, dh, dw), (away, dw, dh)):
            for pid in cur[side_tri]:
                if pid in on:
                    on[pid]["pf"] += pf
                    on[pid]["pa"] += pa
                if pid in open_st:
                    open_st[pid]["pf"] += pf
                    open_st[pid]["pa"] += pa
    if cur_key is not None:
        check(f"{period_label(cur_key[0])} {fmt_clock(cur_key[1])}")
    end_t = max(now_t, 0)
    for pid, st in open_st.items():  # 아직 코트 위 → 지금 시각까지 (진행에 맞춰 막대가 늘어남)
        stints.setdefault(pid, []).append({**st, "e": end_t, "open": True})

    game_sec = end_t
    players = {tri: [] for tri in teams}
    for pid, meta in info.items():
        sts = stints.get(pid, [])
        if not sts:
            continue
        sec = sum(x["e"] - x["s"] for x in sts)
        tri, opp = meta["team"], (away if meta["team"] == home else home)
        o = on[pid]
        on_poss = o["fga"] - o["oreb"] + o["tov"] + 0.44 * o["fta"]
        on_dposs = o["o_fga"] - o["o_oreb"] + o["o_tov"] + 0.44 * o["o_fta"]
        T_, O_ = tot[tri], tot[opp]
        all_poss = T_["fga"] - T_["oreb"] + T_["tov"] + 0.44 * T_["fta"]
        all_dposs = O_["fga"] - O_["oreb"] + O_["tov"] + 0.44 * O_["fta"]
        players[tri].append({
            "id": pid, "name": meta["name"], "starter": meta["starter"], "sec": round(sec),
            "stints": [{"s": round(x["s"], 1), "e": round(x["e"], 1), "pf": x["pf"], "pa": x["pa"],
                        **({"open": True} if x.get("open") else {})} for x in sts],
            "on": {"sec": round(sec), "pf": o["pf"], "pa": o["pa"], "poss": round(on_poss, 1), "dposs": round(on_dposs, 1)},
            "off": {"sec": round(max(game_sec - sec, 0)), "pf": T_["pts"] - o["pf"], "pa": O_["pts"] - o["pa"],
                    "poss": round(all_poss - on_poss, 1), "dposs": round(all_dposs - on_dposs, 1)},
        })
    for tri in teams:
        players[tri].sort(key=lambda r: (not r["starter"], -r["sec"]))
    # 같은 경고가 여러 시점에서 반복되면 처음 몇 개만
    uniq = list(dict.fromkeys(warnings))
    return {"players": players, "now_t": end_t, "warnings": uniq[:20], "warning_count": len(uniq)}


def _tally(tot, on, lineup, tri, teams, key) -> None:
    """팀 기록 하나를 팀 합계와, 코트 위 선수들의 온 기록(자기 팀/상대 팀)에 더한다."""
    tot[tri][key] += 1
    opp = teams[1] if tri == teams[0] else teams[0]
    for pid in lineup[tri]:
        if pid in on:
            on[pid][key] += 1
    for pid in lineup[opp]:
        if pid in on:
            on[pid]["o_" + key] += 1


def _mark_freshness(state: dict, eps: dict, failed: list[str], last_ok: dict) -> dict:
    """이번 수신에서 실패한 데이터가 있으면 '갱신 지연' 표시에 필요한 정보를 붙인다."""
    state["last_ok"] = last_ok
    state["stale"] = failed
    times = [last_ok[n] for n in failed if n in last_ok]
    state["stale_since"] = min(times) if times else None
    state["error"] = " / ".join(f"{n}: {eps[n]['error']}" for n in failed) or None
    return state


def _minutes_sec(v) -> float | None:
    """live 'PT16M51.00S' / stats '16:51' → 초. 값이 없으면 None."""
    if not v:
        return None
    if isinstance(v, str) and v.startswith("PT"):
        return clock_sec(v)
    try:
        mm, ss = str(v).split(":")
        return int(mm) * 60 + float(ss)
    except ValueError:
        return None


def _freshest_score(state: dict, eps: dict, fmt: str, home: str, away: str) -> None:
    """점수·쿼터·시계는 스코어보드/박스스코어/플레이바이플레이 중 가장 많이 진행된 쪽을 쓴다.
    (라이브 스코어보드 파일은 몇 분씩 늦게 갱신될 때가 있어, 그것만 보면 맨 위 점수판이 멈춰 보인다)"""
    cands = []
    if state["period"]:
        cands.append((elapsed(state["period"], clock_sec(eps["scoreboard"]["data"].get("gameClock"))), 0,
                      state["score"][home], state["score"][away], state["period"],
                      eps["scoreboard"]["data"].get("gameClock"), None))
    box = eps.get("boxscore", {})
    if fmt == "live" and box.get("ok") and box["data"].get("period"):
        b = box["data"]
        cands.append((elapsed(b["period"], clock_sec(b.get("gameClock"))), 2, to_int(b["homeTeam"].get("score")) or 0,
                      to_int(b["awayTeam"].get("score")) or 0, b["period"], b.get("gameClock"), b.get("gameStatus")))
    pbp = eps.get("playbyplay", {})
    if pbp.get("ok") and pbp["data"].get("actions"):
        last = None
        for a in reversed(pbp["data"]["actions"]):
            if to_int(a.get("scoreHome")) is not None:
                last = a
                break
        if last:
            a = pbp["data"]["actions"][-1]
            cands.append((elapsed(a["period"], clock_sec(a["clock"])), 1, int(last["scoreHome"]), int(last["scoreAway"]),
                          a["period"], a["clock"], None))
    if not cands:
        return
    t, _, h, w, period, clock, status = max(cands, key=lambda c: (c[0], c[1]))
    state["score"] = {home: h, away: w}
    state["period"], state["clock"] = period, fmt_clock(clock_sec(clock))
    if status == 3 and state["status"] == "live":
        state["status"] = "final"
    state["score_source"] = {0: "scoreboard", 1: "playbyplay", 2: "boxscore"}[max(cands, key=lambda c: (c[0], c[1]))[1]]


def team_stats(st: dict, reb: int) -> dict:
    fgm, fga = st.get("fieldGoalsMade", 0), st.get("fieldGoalsAttempted", 0)
    fg3m, fg3a = st.get("threePointersMade", 0), st.get("threePointersAttempted", 0)
    fta, pts = st.get("freeThrowsAttempted", 0), st.get("points", 0)
    tov = st.get("turnoversTotal", st.get("turnovers", 0))

    def div(a, b):
        return round(a / b, 3) if b else None
    return {"pts": pts, "fgm": fgm, "fga": fga, "fg3m": fg3m, "fg3a": fg3a, "fta": fta, "tov": tov,
            "ast": st.get("assists", 0), "reb": reb,
            "fg_pct": div(fgm, fga), "fg3_pct": div(fg3m, fg3a),
            "efg": div(fgm + 0.5 * fg3m, fga),
            "ts": div(pts, 2 * (fga + 0.44 * fta)),
            "tov_pct": div(tov, fga + 0.44 * fta + tov)}


def game_alerts(acts: list[dict], home: str, away: str, focus: str) -> list[dict]:
    """플레이 목록 전체에서 알림 상황을 찾는다. key 가 같으면 같은 상황이다 (중복 알림 방지용)."""
    out = []
    sh = sa = 0
    streak_team, streak_pts, streak_key, run_alerted = None, 0, None, False
    lead_sign, last_q3_lead_alert_t = 0, -10**9
    clutch_done = set()
    for i, a in enumerate(acts):
        period, clock = a["period"], clock_sec(a["clock"])
        t = elapsed(period, clock)
        label = f"{period_label(period)} {fmt_clock(clock)}"
        h, w = to_int(a.get("scoreHome")), to_int(a.get("scoreAway"))
        if h is not None and w is not None and (h, w) != (sh, sa):
            for team, pts in ((home, h - sh), (away, w - sa)):
                if pts <= 0:
                    continue
                if team != streak_team:
                    streak_team, streak_pts, streak_key, run_alerted = team, 0, f"run:{i}:{team}", False
                streak_pts += pts
                if streak_pts >= RUN_ALERT_PTS and not run_alerted:
                    run_alerted = True
                    out.append({"key": streak_key, "kind": "run", "t": t, "label": label, "team": team,
                                "title": f"{team} {streak_pts}-0 런", "i": i,
                                "body": f"{label} · {away} {w} - {h} {home}"})
            sh, sa = h, w
            margin = (sh - sa) if focus == home else (sa - sh)
            sign = (margin > 0) - (margin < 0)
            leader = focus if sign > 0 else (away if focus == home else home)
            lead_changed = bool(sign and lead_sign and sign != lead_sign)
            if lead_changed and lead_alert_allowed(period, t, last_q3_lead_alert_t):
                alert = {"key": f"lead:{i}", "kind": "lead", "t": t, "label": label, "team": leader,
                         "title": f"역전! {leader} 리드", "body": f"{label} · {away} {sa} - {sh} {home}"}
                if out and out[-1].get("i") == i and out[-1]["kind"] == "run":
                    # 같은 플레이에서 런과 역전이 함께 나오면 알림 하나로 합친다
                    alert["key"] = f"{out[-1]['key']}+lead:{i}"
                    alert["title"] += f" ({out.pop()['title']})"
                out.append(alert)
                if period == 3:
                    last_q3_lead_alert_t = t
            if sign:
                lead_sign = sign
        if period >= 4 and clock <= CLUTCH_CLOCK_SEC and period not in clutch_done and t > 0:
            margin = abs(sh - sa)
            if margin <= CLUTCH_MARGIN and not (clock == 0 and a.get("actionType") in ("period", "game")):
                clutch_done.add(period)
                out.append({"key": f"clutch:{period}", "kind": "clutch", "t": t, "label": label, "team": None,
                            "title": f"접전! {period_label(period)} {fmt_clock(clock)} 남기고 {margin}점 차",
                            "body": f"{away} {sa} - {sh} {home}"})
    return out


def lead_alert_allowed(period: int, t: float, last_q3_alert_t: float) -> bool:
    if period < LEAD_ALERT_FROM_PERIOD:
        return False
    if period == 3:
        return t - last_q3_alert_t >= LEAD_Q3_COOLDOWN
    return True  # 4쿼터·연장


DELAY_FILE = os.path.join(HERE, "delay.json")
DELAY_MAX = 300.0


def load_delay() -> float:
    """중계 영상에 맞춘 딜레이(초). 서버를 다시 켜도 유지된다."""
    try:
        with open(DELAY_FILE) as f:
            return max(0.0, min(DELAY_MAX, float(json.load(f).get("delay_sec", 0))))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def save_delay(v: float) -> float:
    v = round(max(0.0, min(DELAY_MAX, float(v))) * 2) / 2   # 0.5초 단위
    with open(DELAY_FILE, "w") as f:
        json.dump({"delay_sec": v}, f)
    return v


def notify_mac(title: str, body: str) -> None:
    """소리 없는 macOS 알림 배너 (sound name 을 지정하지 않으면 소리가 나지 않는다)."""
    import subprocess

    def q(x: str) -> str:
        return '"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"'
    try:
        subprocess.run(["osascript", "-e", f"display notification {q(body)} with title {q(title)}"],
                       timeout=5, check=False, capture_output=True)
    except Exception:
        pass


def head_to_head(focus: str, opponent: str, before: str | None = None) -> dict:
    """지난 2025-26 정규시즌 맞대결. baseline/game_log.csv 는 읽기만 한다.
    before='YYYY-MM-DD' 이면 그 날짜 이전 경기만 (리플레이할 경기의 결과가 섞이지 않게)."""
    import csv
    if not os.path.exists(BASELINE_GAME_LOG):
        return {"games": [], "note": "지난 시즌 기록 파일이 없습니다."}
    if focus != "NYK":
        return {"games": [], "note": "지난 시즌 기록은 닉스 기준만 있습니다."}
    with open(BASELINE_GAME_LOG, "r", newline="") as f:  # 읽기 전용
        rows = [r for r in csv.DictReader(f) if r["OPPONENT"] == opponent and (not before or r["GAME_DATE"] < before)]
    games = []
    for r in rows:
        tops = _top_scorers(r["GAME_ID"])
        games.append({
            "date": r["GAME_DATE"], "home": r["HOME"] == "True", "wl": r["WL"],
            "pts": int(float(r["PTS"])), "opp_pts": int(float(r["OPP_PTS"])),
            "fg_pct": float(r["FG_PCT"]), "fg3_pct": float(r["FG3_PCT"]),
            "efg": round((int(r["FGM"]) + 0.5 * int(r["FG3M"])) / int(r["FGA"]), 3),
            "reb": int(r["REB"]), "ast": int(r["AST"]), "tov": int(float(r["TOV"])),
            "top": tops,
        })
    w = sum(g["wl"] == "W" for g in games)
    summary = None
    if games:
        summary = {"w": w, "l": len(games) - w,
                   "pts": round(sum(g["pts"] for g in games) / len(games), 1),
                   "opp_pts": round(sum(g["opp_pts"] for g in games) / len(games), 1)}
    return {"season": "2025-26 정규시즌", "opponent": opponent, "games": games, "summary": summary,
            "note": "" if games else f"지난 시즌 {opponent} 와의 경기가 없습니다."}


def _top_scorers(game_id: str) -> dict:
    """맞대결 경기별 양 팀 최다 득점자 (stats API, test_live/h2h_cache 에 저장)."""
    os.makedirs(H2H_CACHE, exist_ok=True)
    path = os.path.join(H2H_CACHE, f"{game_id}.json")
    if not os.path.exists(path):
        try:
            from nba_api.stats.endpoints import boxscoretraditionalv3
            b = boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=game_id, timeout=30).get_dict()
            with open(path, "w") as f:
                json.dump(b, f)
        except Exception:
            return {}
    with open(path) as f:
        b = json.load(f)["boxScoreTraditional"]
    out = {}
    for side in ("homeTeam", "awayTeam"):
        t = b[side]
        best = max(t["players"], key=lambda p: p["statistics"]["points"])
        out[t["teamTricode"]] = {"name": best["nameI"], "pts": best["statistics"]["points"]}
    return out


# ---------------------------------------------------------------- 로그

class PollLogger:
    """폴링마다 polls.jsonl 한 줄, 내용이 바뀐 응답은 raw/ 에 시간 이름으로 저장."""

    def __init__(self, game_id: str, label: str):
        stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.join(LOG_ROOT, label or game_id, stamp)
        os.makedirs(os.path.join(self.dir, "raw"), exist_ok=True)
        self.last_hash: dict[str, str] = {}
        self.n = 0
        self.prev_at: datetime | None = None

    def record(self, raw: dict, state: dict) -> dict:
        at = now_utc()
        self.n += 1
        line = {"poll": self.n, "fetched_utc": at.isoformat(timespec="milliseconds"),
                "fetched_kst": at.astimezone(KST).isoformat(timespec="seconds"),
                "since_prev_poll_sec": round((at - self.prev_at).total_seconds(), 1) if self.prev_at else None,
                "source": raw["source"], "live_error": raw.get("live_error"),
                "status": state.get("status"), "period": state.get("period"), "clock": state.get("clock"),
                "score": state.get("score"), "endpoints": {}}
        self.prev_at = at
        stamp = at.astimezone(KST).strftime("%Y%m%d-%H%M%S")
        for name, res in raw["endpoints"].items():
            entry = {"ok": res["ok"], "ms": res["ms"], "error": res["error"]}
            if res["ok"]:
                body = json.dumps(res["data"], sort_keys=True, ensure_ascii=False)
                h = hashlib.sha1(body.encode()).hexdigest()[:12]
                entry.update(hash=h, bytes=len(body), changed=h != self.last_hash.get(name))
                if entry["changed"]:
                    self.last_hash[name] = h
                    with open(os.path.join(self.dir, "raw", f"{stamp}_{name}.json"), "w") as f:
                        f.write(body)
                if name == "playbyplay":
                    acts = res["data"].get("actions", [])
                    entry["n_actions"] = len(acts)
                    entry["last_action_number"] = acts[-1]["actionNumber"] if acts else None
                    newest = max((a.get("timeActual") for a in acts if a.get("timeActual")), default=None)
                    if newest and not raw["source"].startswith("replay"):  # live 형식만 실제 시각이 있다
                        t = datetime.fromisoformat(newest.replace("Z", "+00:00"))
                        entry["newest_action_utc"] = newest
                        entry["latency_sec"] = round((at - t).total_seconds(), 1)
            line["endpoints"][name] = entry
        with open(os.path.join(self.dir, "polls.jsonl"), "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return line


def report(log_dir: str) -> None:
    path = os.path.join(log_dir, "polls.jsonl")
    with open(path) as f:
        polls = [json.loads(l) for l in f if l.strip()]
    if not polls:
        print("기록 없음")
        return
    ts = [datetime.fromisoformat(p["fetched_utc"]) for p in polls]
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    print(f"로그: {log_dir}")
    print(f"폴링 {len(polls)}회 · {polls[0]['fetched_kst']} ~ {polls[-1]['fetched_kst']}")
    if gaps:
        print(f"폴링 간격(초): 평균 {statistics.mean(gaps):.1f} · 최소 {min(gaps):.1f} · 최대 {max(gaps):.1f}")
    sources = {}
    for p in polls:
        sources[p["source"]] = sources.get(p["source"], 0) + 1
    print("데이터 소스:", ", ".join(f"{k} {v}회" for k, v in sources.items()))
    live_err = next((p["live_error"] for p in polls if p.get("live_error")), None)
    if live_err:
        print(f"  live 엔드포인트 실패 예: {live_err}")
    for name in ("scoreboard", "boxscore", "playbyplay"):
        changes = [(t, p["endpoints"][name]) for t, p in zip(ts, polls)
                   if name in p["endpoints"] and p["endpoints"][name].get("changed")]
        fails = sum(1 for p in polls if name in p["endpoints"] and not p["endpoints"][name]["ok"])
        if not changes and not fails:
            continue
        cg = [(b[0] - a[0]).total_seconds() for a, b in zip(changes, changes[1:])]
        line = f"[{name}] 새 데이터 {len(changes)}회, 실패 {fails}회"
        if cg:
            line += f" · 새 데이터 간격(초) 평균 {statistics.mean(cg):.1f} / 중앙값 {statistics.median(cg):.1f} / 최대 {max(cg):.1f}"
        print(line)
        if name == "playbyplay":
            grows = [c[1].get("n_actions", 0) for c in changes]
            if len(grows) > 1:
                inc = [b - a for a, b in zip(grows, grows[1:])]
                print(f"  갱신당 새 플레이 수: 평균 {statistics.mean(inc):.1f} · 최대 {max(inc)}")
            lat = [p["endpoints"]["playbyplay"].get("latency_sec") for p in polls
                   if "playbyplay" in p["endpoints"] and p["endpoints"]["playbyplay"].get("latency_sec") is not None]
            if lat:
                print(f"  지연(받은 시각 − 최신 플레이 실제 시각, 초): 중앙값 {statistics.median(lat):.1f} · 최소 {min(lat):.1f}")
            else:
                print("  지연 시간: 이 소스에는 플레이의 실제 시각(timeActual)이 없어 계산할 수 없습니다.")


# ---------------------------------------------------------------- 오늘의 브리핑

import briefing  # noqa: E402  (같은 폴더)


def parse_offsets(text: str) -> list[float]:
    """'60m,5m' / '8s,3s' → 초 목록."""
    out = []
    for part in filter(None, (x.strip() for x in text.split(","))):
        unit = part[-1]
        out.append(float(part[:-1]) * (60 if unit == "m" else 3600 if unit == "h" else 1) if unit in "smh" else float(part))
    return out


class BriefingManager:
    """실행 시 한 번 만들고, 팁오프 N초 전마다 자동 갱신한다 (데이터 부분만; 한국어 편집본은 그대로)."""

    def __init__(self, game_id: str, before: list[float], log_dir: str, expected_override: list[str] | None):
        self.game_id, self.before, self.log_dir = game_id, sorted(before, reverse=True), log_dir
        self.expected_override = expected_override
        self.expected_hook = None
        self.data: dict | None = None
        self.started = now_utc()
        self.done: set[float] = set()
        self.last_reason = None
        self.refreshing = False
        self.lineup_result: dict | None = None
        self.next_utc: str | None = None
        self.lock = threading.Lock()
        self._refresh("실행 시작")

    def _refresh(self, reason: str) -> None:
        if self.refreshing:
            return
        self.refreshing = True

        def run():
            t0 = time.monotonic()
            try:
                data = briefing.build(self.game_id, on_recover=lambda: self._refresh("못 받은 데이터 다시 받음"))
                err = data.get("errors")
            except Exception as exc:  # 전체 실패 시 직전 브리핑 유지
                data, err = None, [f"{type(exc).__name__}: {exc}"]
                import resilient
                resilient.log_error("브리핑", exc, self.game_id)
            with self.lock:
                if data:
                    self.data = data
                self.last_reason = reason
                self.refreshing = False
            line = {"at_kst": datetime.now(KST).isoformat(timespec="seconds"), "reason": reason,
                    "ok": data is not None, "errors": err, "sec": round(time.monotonic() - t0, 1),
                    "news": len(data["news_feed"]) if data else None,
                    "new_since_editorial": len(data.get("new_since_editorial", [])) if data else None}
            with open(os.path.join(self.log_dir, "briefing_refresh.jsonl"), "a") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            print(f"  ◆ 브리핑 갱신 ({reason}): {'성공' if data else '실패'} {err or ''}", flush=True)
        threading.Thread(target=run, daemon=True).start()

    def tick(self, state: dict) -> None:
        tip = state.get("tipoff_utc")
        if not tip:
            return
        tip_t = datetime.fromisoformat(tip.replace("Z", "+00:00"))
        now = now_utc()
        upcoming = []
        for off in self.before:
            due = tip_t - timedelta(seconds=off)
            if off in self.done:
                continue
            if due <= self.started:      # 실행 전에 이미 지난 시점은 시작 시 생성으로 대신한다
                self.done.add(off)
            elif now >= due:
                self.done.add(off)
                self._refresh(f"팁오프 {self._fmt(off)} 전 자동 갱신")
            else:
                upcoming.append(due)
        self.next_utc = min(upcoming).isoformat(timespec="seconds") if upcoming else None

    @staticmethod
    def _fmt(sec: float) -> str:
        return f"{sec / 60:g}분" if sec >= 60 else f"{sec:g}초"

    def expected_names(self) -> list[str]:
        if self.expected_override:
            return self.expected_override
        if self.expected_hook:            # 출처 기반 선발 예측(lineups.py)이 있으면 그것과 비교
            names = self.expected_hook()
            if names:
                return names
        lu = (self.data or {}).get("lineup") or {}
        return [e["name"] for e in lu.get("expected", [])]

    def snapshot(self) -> dict | None:
        with self.lock:
            if not self.data:
                return {"loading": True, "next_refresh_utc": self.next_utc}
            return {**self.data, "last_reason": self.last_reason, "next_refresh_utc": self.next_utc,
                    "refreshing": self.refreshing, "lineup_result": self.lineup_result,
                    "expected_override": self.expected_override}


# ---------------------------------------------------------------- 폴링 루프 + 서버

def _turnover_summary(raw: dict, state: dict) -> dict | None:
    """라이브 화면용 양 팀 실책 수·실책 기반 득점 (지금까지의 플레이바이플레이로 계산)."""
    pbp = raw["endpoints"].get("playbyplay") or {}
    if raw.get("format") != "live" or not pbp.get("ok") or state.get("status") not in ("live", "final"):
        return None
    import turnovers
    gid = pbp["data"].get("gameId") or state.get("game_id")
    return {**turnovers.live_summary(pbp["data"]["actions"], state["home"], state["away"]),
            "fastbreak": raw.get("source") != "ESPN",   # ESPN 플레이 기록에는 속공 표시가 없다
            "computed_utc": now_utc().isoformat(timespec="seconds"),
            "source": {"name": "NBA.com 플레이바이플레이", "url": f"https://www.nba.com/game/{gid}/play-by-play"}}


class Tracker:
    def __init__(self, source, game_id: str, poll_sec: float, log_label: str, mode_label: str):
        self.source, self.poll_sec, self.mode_label = source, poll_sec, mode_label
        self.logger = PollLogger(game_id, log_label)
        self.state: dict = {"status": "unknown", "message": "첫 데이터를 받는 중…"}
        self.meta = {"poll_sec": poll_sec, "mode": mode_label, "log_dir": os.path.relpath(self.logger.dir, HERE)}
        self.lock = threading.Lock()
        self.stopped = False
        self.notify = False
        self.sent: set[str] | None = None   # None = 아직 첫 폴링 전
        self.h2h: dict | None = None
        self.h2h_for: str | None = None
        self.brief: BriefingManager | None = None
        self.extra_alerts: list[dict] = []   # 선발 변경처럼 플레이바이플레이와 무관한 알림
        self.alert_hook = None               # (raw, state, tracker) -> 알림 목록 (예: 빌런 알림)
        self.wake = threading.Event()        # 재생 제어 직후 바로 다시 받기
        self.done_cv = threading.Condition()
        self.seen_gen = 0                    # 마지막으로 계산한 화면의 슬라이더 이동 번호
        self.quiet_keys: set[str] = set()    # 슬라이더로 건너뛰어 조용히 추가된 알림
        self.delay_sec = 0.0                  # 중계 영상에 맞춰 화면·알림을 늦추는 시간 (라이브만)
        self.history: list[tuple[float, dict]] = []   # (받은 시각 monotonic, 화면 상태)
        self.src_seen: str | None = None     # 직전 폴링의 소스 (cdn ↔ ESPN ↔ stats 가 바뀌면 알림 키가 달라진다)

    def _restore(self) -> None:
        """정식 라이브: 서버를 다시 켰을 때 마지막 성공 응답으로 화면을 먼저 채운다 (전부 '갱신 지연' 표시)."""
        lg = getattr(self.source, "last_good", None)
        raw = lg.restore() if lg else None
        if not raw:
            return
        try:
            state = normalize(raw, None)
        except Exception as exc:
            _warn_once(f"저장본 복원 실패: {exc}")
            return
        saved = raw["saved"]
        state["last_ok"] = {n: saved[n] for n in saved}
        state["stale"] = list(raw["endpoints"])
        state["stale_since"] = min(saved.values())
        state["restored_from"] = min(saved.values())
        state["error"] = "서버 재시작 전 저장본"
        for key, fn in (("turnovers", _turnover_summary), ("assist_net", assist_network)):
            try:
                state[key] = fn(raw, state)
            except Exception:
                state[key] = None
        self.state = state
        self.meta["restored_from"] = state["restored_from"]
        print(f"  저장본 복원: {raw['source']} · {state.get('period_label', '')} {state.get('clock', '')} {state.get('score', '')}", flush=True)

    def run(self) -> None:
        finals = 0
        fail_streak = 0
        self._restore()
        while not self.stopped:
            t0 = time.monotonic()
            raw = self.source.fetch()
            fetched_utc = now_utc().isoformat(timespec="milliseconds")
            lg = getattr(self.source, "last_good", None)
            if lg:
                try:
                    lg.save(raw)
                except Exception as exc:
                    _warn_once(f"마지막 성공 응답 저장 실패: {exc}")
            gen = raw.get("seek_gen", 0)
            # 슬라이더로 이동한 뒤 첫 계산: 이전 화면을 이어 쓰지 않고 그 시점만으로 새로 계산하고,
            # 그사이 새로 생긴 알림은 Mac 알림 없이 목록에만 조용히 넣는다
            quiet = gen != self.seen_gen
            with self.lock:
                state = normalize(raw, None if quiet else self.state)
            line = self.logger.record(raw, state)
            if self.brief:
                self.brief.tick(state)
                self._check_lineup(state)
            hooked = []
            if self.alert_hook:
                try:
                    hooked = self.alert_hook(raw, state, self)
                except Exception as exc:
                    print(f"  ! 추가 알림 계산 실패: {exc}", flush=True)
            merged = {a["key"]: a for a in self.extra_alerts + hooked + state.get("alerts", [])}   # 직전 내용을 이어 쓸 때 중복 방지
            state["alerts"] = sorted(merged.values(), key=lambda a: a["t"])
            # 소스가 바뀐 첫 폴링: 그동안 지나간 상황이 새 알림처럼 보이므로 Mac 알림 없이 목록에만 넣는다
            switched = self.src_seen is not None and raw["source"] != self.src_seen and any(r["ok"] for r in raw["endpoints"].values())
            if any(r["ok"] for r in raw["endpoints"].values()):
                self.src_seen = raw["source"]
            self._handle_alerts(state, quiet or switched)
            for a in state["alerts"]:
                if a["key"] in self.quiet_keys:
                    a["quiet"] = True
            pbp_ok = (raw["endpoints"].get("playbyplay") or {}).get("ok")
            prev = None if quiet else self.state
            for key, fn in (("assist_net", assist_network), ("turnovers", _turnover_summary)):
                try:
                    state[key] = fn(raw, state)
                except Exception as exc:
                    state[key] = None
                    _warn_once(f"{key} 계산 실패: {exc}")
                if state[key] is None and not pbp_ok and prev and prev.get(key):
                    state[key] = prev[key]    # 플레이바이플레이를 못 받았으면 직전 값 유지 (갱신 지연 표시는 stale 로)
            self._ensure_h2h(state)
            if hasattr(self.source, "poll_interval"):
                interval = self.source.poll_interval(self.poll_sec)
            elif state.get("status") == "live" and hasattr(self.source, "live_interval"):
                interval, host, why = self.source.live_interval()   # 출처별: ESPN 10초(문제 있으면 20·30초) · cdn 15초
                NH.set_live_every(host, interval, why)
            else:
                interval = min(self.poll_sec, LIVE_POLL_SEC) if state.get("status") == "live" else self.poll_sec
            backoff = None
            if hasattr(self.source, "last_good"):   # 정식 라이브만 (리플레이는 NBA 서버에 묻지 않는다)
                if state.get("status") == "live":
                    NH.live_on(self.source.game_id)
                elif state.get("status") == "final":
                    NH.live_off(self.source.game_id)
                # 하나도 못 받은 폴링이 이어지면, NBA 서버가 다시 열릴 때까지 기다린다 (30 → 60 → 120 → 300초 + 지터)
                fail_streak = 0 if any(r["ok"] for r in raw["endpoints"].values()) else fail_streak + 1
                if fail_streak:
                    interval = max(interval, NH.next_try_in(), 5)
                backoff = {"fail_streak": fail_streak, "next_poll_in_sec": round(interval)}
            state["clock_run"] = self._clock_run(raw, state, fetched_utc, interval, quiet)
            with self.lock:
                self.state = state
                self.history.append((time.monotonic(), state))
                cutoff = time.monotonic() - DELAY_MAX - 30
                while len(self.history) > 2 and self.history[1][0] < cutoff:
                    self.history.pop(0)
                self.meta["backoff"] = backoff
                self.meta["espn_error"] = raw.get("espn_error")
                self.meta["espn_match"] = raw.get("espn_match")
                self.meta["espn_event"] = raw.get("espn_event")
                self.meta.update(last_poll_utc=line["fetched_utc"], source=raw["source"],
                                 live_error=raw.get("live_error"), polls=line["poll"],
                                 next_poll_utc=(now_utc() + timedelta(seconds=interval)).isoformat(),
                                 issues_detected=len(state.get("alerts", [])))
                if hasattr(self.source, "replay_info"):
                    self.meta["replay"] = self.source.replay_info()
            with self.done_cv:
                self.seen_gen = gen
                self.done_cv.notify_all()
            print(f"[{line['fetched_kst']}] {raw['source']:<14} {state.get('status'):<8} "
                  f"{state.get('period_label', '')} {state.get('clock', '')} {state.get('score', '')}", flush=True)
            if state.get("status") == "final":
                finals += 1
                if finals > STOP_AFTER_FINAL_POLLS:
                    print("경기 종료 확인 — 폴링을 멈춥니다. 화면은 계속 열어 둘 수 있습니다 (Ctrl+C 로 종료).")
                    with self.lock:
                        self.meta["stopped"] = True
                    if not hasattr(self.source, "control"):
                        return
                    # 리플레이: 슬라이더로 다시 앞쪽으로 옮기면 그때부터 다시 계산한다
                    while not self.stopped and self.source.replay_info()["pos"] >= self.source.total \
                            and self.source.seek_gen == self.seen_gen:
                        self.wake.wait(1)
                        self.wake.clear()
                    finals = 0
                    with self.lock:
                        self.meta["stopped"] = False
                    continue
            else:
                finals = 0
            self.wake.wait(max(interval - (time.monotonic() - t0), 0))
            self.wake.clear()

    def _clock_run(self, raw: dict, state: dict, at: str, interval: float, reset: bool) -> dict | None:
        """화면 시계 자동 진행용 정보. 직전 두 번의 데이터에서 같은 쿼터 시계가 줄었으면 running —
        화면은 다음 데이터가 올 때까지 at 부터 rate 배속으로 1초씩 줄인다 (0 아래로는 안 내려감, hold_sec 넘게 새 데이터가
        없으면 멈춤). 같은 시계가 두 번 연속이면 멈춘 것(파울·타임아웃). 쿼터 사이·하프타임·경기 전후에는 진행하지 않는다."""
        sb = raw["endpoints"].get("scoreboard") or {}
        if not sb.get("ok"):          # 이번에 새 시계를 못 받음: 직전 정보를 그대로 (hold_sec 가 지나면 화면이 알아서 멈춘다)
            return (self.state or {}).get("clock_run")
        sec = clock_sec_label(state.get("clock"))
        cur = (state.get("period"), sec)
        prev = None if reset else getattr(self, "clock_prev", None)
        self.clock_prev = cur
        running = (state.get("status") == "live" and not state.get("break") and prev is not None
                   and prev[0] == cur[0] and 0 < cur[1] < prev[1])
        rate = 1.0
        if hasattr(self.source, "replay_info"):   # 리플레이: 배속만큼 (일시정지면 멈춤)
            info = self.source.replay_info()
            rate = float(info["speed"]) if info["playing"] else 0.0
            running = running and rate > 0
        return {"running": running, "sec": sec, "at": at, "rate": rate, "hold_sec": round(interval + 10, 1)}

    def _handle_alerts(self, state: dict, quiet: bool = False) -> None:
        alerts = state.get("alerts", [])
        if self.sent is None:
            # 실행 전에 이미 지나간 상황은 알리지 않는다 (경기 도중 켰을 때 알림 폭주 방지)
            self.sent = {a["key"] for a in alerts if a["kind"] != "lineup"}
            alerts = [a for a in alerts if a["kind"] == "lineup"]
            if not alerts:
                return
        fresh = [a for a in alerts if a["key"] not in self.sent]
        for i, a in enumerate(fresh):
            self.sent.add(a["key"])
            if quiet:
                self.quiet_keys.add(a["key"])
            delivered = self.notify and i < MAX_NOTIFY_PER_POLL and not quiet
            if delivered:
                if self.delay_sec > 0:
                    threading.Timer(self.delay_sec, notify_mac, (a["title"], a["body"])).start()
                else:
                    notify_mac(a["title"], a["body"])
            with open(os.path.join(self.logger.dir, "alerts.jsonl"), "a") as f:
                f.write(json.dumps({**a, "at_kst": datetime.now(KST).isoformat(timespec="seconds"),
                                    "mac_notified": delivered, "skipped_by_seek": quiet}, ensure_ascii=False) + "\n")
            print(f"  {'·' if quiet else '▶'} 알림{' (건너뛴 구간, 조용히)' if quiet else ''}: {a['title']} — {a['body']}", flush=True)

    def _check_lineup(self, state: dict) -> None:
        """팁오프 후 실제 선발이 확정되면 예상과 비교해, 다르면 '선발 변경' 알림을 한 번 보낸다."""
        b = self.brief
        if b.lineup_result is not None or state.get("status") not in ("live", "final"):
            return
        expected = b.expected_names()
        actual = [p["name"] for p in state.get("players", []) if p["team"] == state.get("focus") and p.get("starter")]
        if not expected or len(actual) != 5:
            return
        diff = briefing.lineup_diff(expected, actual)
        b.lineup_result = {"expected": expected, "actual": actual, "diff": diff,
                           "checked_utc": now_utc().isoformat(timespec="seconds")}
        if diff:
            body = f"{', '.join(diff['in'])} 선발 출전 (예상했던 {', '.join(diff['out'])} 대신)"
            self.extra_alerts.append({"key": f"lineup:{b.game_id}", "kind": "lineup", "t": 0, "label": "팁오프",
                                      "team": state["focus"], "title": f"{state['focus']} 선발 변경", "body": body})

    def _ensure_h2h(self, state: dict) -> None:
        opp = state.get("opponent")
        if not opp or opp == self.h2h_for:
            return
        self.h2h_for = opp

        def load():
            data = head_to_head(state["focus"], opp)
            with self.lock:
                self.h2h = data
        threading.Thread(target=load, daemon=True).start()

    def control(self, cmd: dict) -> dict:
        if not hasattr(self.source, "control"):
            return {"error": "라이브 모드에서는 재생 제어를 할 수 없습니다"}
        seek = cmd.get("action") == "seek"
        info = self.source.control(cmd.get("action"), cmd.get("value"))
        print(f"  ■ 재생 제어: {cmd.get('action')} {cmd.get('value') or ''} → {info['speed']:g}배속 "
              f"{'재생' if info['playing'] else '일시정지'}", flush=True)
        with self.lock:
            self.meta["replay"] = info
        self.wake.set()
        if seek:   # 그 시점 화면이 다 계산될 때까지 기다렸다가 답한다 (화면 전체가 같은 시점이 되도록)
            target = self.source.seek_gen
            with self.done_cv:
                self.done_cv.wait_for(lambda: self.seen_gen >= target, timeout=5)
            info = self.source.replay_info()
        return info

    def view_state(self) -> dict:
        """화면에 보여 줄 상태 — 딜레이가 있으면 그만큼 전에 받은 상태 (lock 안에서 부른다)."""
        if self.delay_sec <= 0 or not self.history:
            return self.state
        target = time.monotonic() - self.delay_sec
        shown = None
        for t, st in self.history:
            if t <= target:
                shown = st
            else:
                break
        if shown is None:   # 서버를 막 켜서 딜레이만큼 쌓인 기록이 아직 없을 때
            return {"status": "unknown", "message": f"중계 딜레이 {self.delay_sec:g}초 맞추는 중…"}
        return shown

    def snapshot(self) -> dict:
        with self.lock:
            meta = dict(self.meta)
            meta["delay_sec"] = self.delay_sec
            if hasattr(self.source, "replay_info"):
                meta["replay"] = self.source.replay_info()  # 재생 위치는 요청 시점 기준으로 바로 계산
            return {"state": self.view_state(), "meta": meta, "h2h": self.h2h,
                    "briefing": self.brief.snapshot() if self.brief else None,
                    "server_utc": now_utc().isoformat()}


def find_game(team: str, now: datetime | None = None) -> dict:
    """팀의 '지금 볼 경기'를 찾는다.

    1) 오늘 라이브 스코어보드에 그 팀 경기가 있고 아직 안 끝났으면 그 경기
    2) 아니면 시즌 일정에서 팁오프가 지금 이후(진행 중 포함, 4시간 여유)인 가장 가까운 경기
    """
    use_scoreboard = now is None  # 시각을 지정한 테스트에서는 일정만 본다
    now = now or now_utc()
    finished: set[str] = set()     # 스코어보드에서 이미 끝난 것으로 확인된 경기
    if use_scoreboard:
        try:
            from nba_api.live.nba.endpoints import scoreboard
            for g in scoreboard.ScoreBoard(headers=BROWSER_HEADERS, timeout=20).get_dict()["scoreboard"]["games"]:
                tris = (g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"])
                if team in tris and g["gameStatus"] == 3:
                    finished.add(g["gameId"])
                elif team in tris:
                    return {"game_id": g["gameId"], "away": tris[1], "home": tris[0],
                            "tipoff_utc": g["gameTimeUTC"], "how": "오늘 스코어보드"}
        except Exception:
            pass  # 막혀 있으면 일정에서 찾는다
    from nba_api.stats.endpoints import scheduleleaguev2
    start_year = now.year if now.month >= 8 else now.year - 1
    df = scheduleleaguev2.ScheduleLeagueV2(season=f"{start_year}-{(start_year + 1) % 100:02d}",
                                           timeout=30).get_data_frames()[0]
    df = df[(df["homeTeam_teamTricode"] == team) | (df["awayTeam_teamTricode"] == team)].copy()
    df["tip"] = df["gameDateTimeUTC"].map(lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")))
    df = df[(df["tip"] >= now - timedelta(hours=4)) & ~df["gameId"].isin(finished)].sort_values("tip")
    if df.empty:
        raise SystemExit(f"{team} 의 남은 경기를 일정에서 찾지 못했습니다. --game 으로 직접 지정해 주세요.")
    r = df.iloc[0]
    return {"game_id": r["gameId"], "away": r["awayTeam_teamTricode"], "home": r["homeTeam_teamTricode"],
            "tipoff_utc": r["gameDateTimeUTC"], "how": "시즌 일정"}


def completed_games(team: str, now: datetime | None = None) -> list[dict]:
    """팀의 끝난 경기 목록 (최신순). 이번 시즌 + 지난 시즌, 프리시즌·정규시즌·플레이오프."""
    from nba_api.stats.endpoints import teamgamelogs
    from nba_api.stats.static import teams
    now = now or now_utc()
    tid = teams.find_team_by_abbreviation(team)["id"]
    start = now.year if now.month >= 8 else now.year - 1
    out = []
    for y in (start, start - 1):
        season = f"{y}-{(y + 1) % 100:02d}"
        for stype, label in (("Pre Season", "프리시즌"), ("Regular Season", "정규시즌"), ("Playoffs", "플레이오프")):
            time.sleep(0.4)
            try:
                df = teamgamelogs.TeamGameLogs(team_id_nullable=tid, season_nullable=season,
                                               season_type_nullable=stype, timeout=30).get_data_frames()[0]
            except Exception:
                continue
            for r in df.itertuples():
                opp = r.MATCHUP.split()[-1]
                out.append({"game_id": r.GAME_ID, "date": r.GAME_DATE[:10], "matchup": r.MATCHUP, "opp": opp,
                            "home": "vs." in r.MATCHUP, "wl": r.WL, "pts": int(r.PTS),
                            "opp_pts": int(r.PTS - r.PLUS_MINUS), "type": label, "season": season})
    return sorted(out, key=lambda g: g["date"], reverse=True)


def pick_replay_game(team: str, vs: str | None, date: str | None) -> dict:
    games = completed_games(team)
    if vs:
        games = [g for g in games if g["opp"] == vs.upper()]
    if date:
        if "/" in date:   # 10/5 → 가장 최근의 10월 5일
            m, d = (int(x) for x in date.split("/"))
            games = [g for g in games if int(g["date"][5:7]) == m and int(g["date"][8:10]) == d]
        else:
            games = [g for g in games if g["date"] == date]
    if not games:
        raise SystemExit("조건에 맞는 끝난 경기가 없습니다. --list 로 목록을 확인해 주세요.")
    return games[0]


def print_game_list(team: str, n: int = 15) -> None:
    games = completed_games(team)[:n]
    print(f"{team} 최근 경기 (최신순)")
    for g in games:
        res = "승" if g["wl"] == "W" else "패"
        where = "홈" if g["home"] else "원정"
        print(f"  {g['date']}  {g['type']:<5}  {where:<2} vs {g['opp']:<3}  {res} {g['pts']}-{g['opp_pts']}   --game {g['game_id']}")
    print("다시 보기: python3 test_live/tracker.py replay --speed 10 --lan --game <경기ID>  (또는 --vs 팀, --date 월/일)")


def local_hostname() -> str | None:
    """macOS 로컬 호스트 이름 (IP 가 바뀌어도 같은 와이파이에서 <이름>.local 로 접속된다)."""
    import subprocess
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3).stdout.strip()
        return name or None
    except Exception:
        return None


def lan_ip() -> str | None:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 80))  # 실제로 패킷을 보내지 않고 기본 경로의 내 IP 만 확인
            return sk.getsockname()[0]
    except OSError:
        return None


def serve(tracker: Tracker, port: int, open_browser: bool, lan: bool = False) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.path != "/api/control":
                self.send_response(404)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", 0))
            try:
                cmd = json.loads(self.rfile.read(n) or b"{}")
                body = json.dumps(tracker.control(cmd), ensure_ascii=False).encode()
                code = 200
            except Exception as exc:
                body, code = json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), 400
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                body = json.dumps(tracker.snapshot(), ensure_ascii=False).encode()
                ctype = "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    body = f.read()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    threading.Thread(target=tracker.run, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"대시보드: {url}")
    if lan:
        ip, host = lan_ip(), local_hostname()
        fixed = f"http://{host}.local:{port}/" if host else None
        by_ip = f"http://{ip}:{port}/" if ip else None
        print(f"폰(같은 와이파이): {fixed or by_ip or '주소를 찾지 못했습니다'}" + (f"  (IP 주소: {by_ip})" if fixed and by_ip else ""))
        with tracker.lock:
            tracker.meta["phone_url"] = fixed or by_ip
    print(f"Mac 알림: {'켜짐 (소리 없음)' if tracker.notify else '꺼짐'}\n로그: {tracker.logger.dir}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        tracker.stopped = True
        print("\n종료. 수집 간격 분석:")
        report(tracker.logger.dir)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lv = sub.add_parser("live")
    lv.add_argument("--game", default=None, help="경기 ID (생략하면 --team 의 오늘/다음 경기를 자동으로 찾음)")
    lv.add_argument("--team", default=FOCUS, help="자동으로 찾을 팀 (기본 NYK)")
    lv.add_argument("--poll", type=float, default=30)
    lv.add_argument("--source", choices=["auto", "live", "stats"], default="auto",
                    help="auto: live 엔드포인트 우선, 막히면 stats (기본값)")
    rp = sub.add_parser("replay")
    rp.add_argument("--game", default=None, help="경기 ID (생략하면 가장 최근에 끝난 경기)")
    rp.add_argument("--team", default=FOCUS)
    rp.add_argument("--vs", default=None, help="상대팀으로 고르기 (예: PHI) — 가장 최근 맞대결")
    rp.add_argument("--date", default=None, help="날짜로 고르기 (예: 10/5 또는 2026-10-05)")
    rp.add_argument("--list", action="store_true", help="최근 경기 목록만 보여 주고 끝내기")
    rp.add_argument("--speed", type=float, default=30)
    rp.add_argument("--poll", type=float, default=3)
    rp.add_argument("--pregame", type=float, default=12, help="'경기 대기 중' 화면을 보여 줄 시간(초)")
    rp.add_argument("--format", choices=["live", "stats"], default="live", help="흉내 낼 응답 형식")
    rp.add_argument("--real-pace", action="store_true",
                    help="시험용: 플레이의 실제 발생 시각대로 재생 (파울·타임아웃·쿼터 휴식에 시계가 멈춤, --speed 1 이 실제 속도)")
    rp.add_argument("--fail", action="append", default=[], metavar="엔드포인트@시작-끝",
                    help="테스트용 장애 주입, 예: playbyplay@20-28 (실행 시작 후 실제 초)")
    rt = sub.add_parser("report")
    rt.add_argument("log_dir")
    for p in (lv, rp):
        p.add_argument("--port", type=int, default=8766)
        p.add_argument("--no-open", action="store_true")
        p.add_argument("--lan", action="store_true", help="같은 와이파이의 폰에서도 접속 가능하게")
    lv.add_argument("--no-notify", action="store_true", help="Mac 알림 끄기")
    for p in (lv, rp):
        p.add_argument("--brief-before", default="60m,5m", help="브리핑 자동 갱신 시점 (팁오프 기준, 예: 60m,5m)")
        p.add_argument("--no-brief", action="store_true", help="오늘의 브리핑 끄기")
        p.add_argument("--expected-starters", default="", help="테스트용: 예상 선발을 직접 지정 (쉼표 구분, 예: J. Brunson,...)")
    rp.add_argument("--brief-game", default=None, help="리플레이 중 브리핑을 보여 줄 경기 ID (기본: 브리핑 없음)")
    rp.add_argument("--notify", action="store_true", help="리플레이에서도 Mac 알림 띄우기")
    args = ap.parse_args()

    if args.cmd == "report":
        return report(args.log_dir)
    if args.cmd == "live":
        if not args.game:
            g = find_game(args.team)
            tip_kst = datetime.fromisoformat(g["tipoff_utc"].replace("Z", "+00:00")).astimezone(KST)
            print(f"경기 자동 선택({g['how']}): {g['away']} @ {g['home']} · {g['game_id']} · "
                  f"팁오프 {tip_kst:%m월 %d일 %H:%M} KST")
            args.game = g["game_id"]
        tracker = Tracker(AutoSource(args.game, args.source), args.game, args.poll, args.game, f"live/{args.source}")
    else:
        if args.list:
            return print_game_list(args.team)
        if not args.game:
            g = pick_replay_game(args.team, args.vs, args.date)
            res = "승" if g["wl"] == "W" else "패"
            print(f"다시 볼 경기: {g['date']} {g['type']} {g['matchup']} ({res} {g['pts']}-{g['opp_pts']}) · {g['game_id']}")
            args.game = g["game_id"]
        faults = []
        for f in args.fail:
            name, span = f.split("@")
            a, b = span.split("-")
            faults.append((name, float(a), float(b)))
        src = ReplaySource(args.game, args.speed, args.pregame, args.format, faults, args.real_pace)
        tracker = Tracker(src, args.game, args.poll, f"replay-{args.game}", f"replay({args.format}) ×{args.speed:g}")
    tracker.notify = (not args.no_notify) if args.cmd == "live" else args.notify
    if not args.no_brief and (args.cmd == "live" or args.brief_game):
        brief_game = args.game if args.cmd == "live" else args.brief_game
        override = [x.strip() for x in args.expected_starters.split(",") if x.strip()] or None
        tracker.brief = BriefingManager(brief_game, parse_offsets(args.brief_before), tracker.logger.dir, override)
    serve(tracker, args.port, not args.no_open, args.lan)


if __name__ == "__main__":
    main()
