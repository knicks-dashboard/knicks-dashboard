"""한 페이지 대시보드 서버 — [지금 경기] [지난 경기] [전전 경기].

  python3 test_live/app.py            (한 번 켜 두면 계속 돈다, 같은 와이파이의 폰에서도 접속)

- 경기는 NBA 일정에서 자동으로 고른다 (30초마다 상태 확인, 10분마다 일정 새로 받기).
- 지금 경기: 팁오프 1시간 전까지는 프리뷰(브리핑·예측·맞대결), 그 뒤엔 라이브 추적.
  경기가 끝나고 15분이 지나면 다음 경기 프리뷰로 넘어가고, 끝난 경기는 '지난 경기'가 된다.
- 지난/전전 경기: 재생 버튼을 누르면 리플레이 (1x/5x/10x/30x, 일시정지).
- 라이브와 리플레이는 tracker.py 의 같은 엔진·같은 화면 데이터 형식을 쓴다.
- 모든 쓰기는 test_live/ 안에서만. baseline/game_log.csv 는 맞대결 비교에 읽기만 한다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import league as LG
import resilient as RS
import desk as DK
import fatigue as FT
import postgame as PG
import turnovers as TV
import lineups as LU
import nba_http as NH
import rivals as R
import tracker as T

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "briefing", "cache")
LIVE_START_BEFORE = timedelta(minutes=60)     # 팁오프 1시간 전부터 라이브 추적 (08:30 경기면 한국 07:30)
NEXT_GAME_AFTER_FINAL = timedelta(minutes=15)  # 경기 종료 15분 뒤 다음 경기로
SCHEDULE_REFRESH = timedelta(minutes=60)        # 평소 (일정은 자주 바뀌지 않는다)
SCHEDULE_REFRESH_NEAR = timedelta(minutes=10)   # 닉스 경기 앞뒤 (팁오프 2시간 전 ~ 5시간 뒤): 경기 상태가 바뀌는 때
TICK_SEC = 30
INJURY_LIVE_EVERY = timedelta(minutes=5)       # 닉스 라이브 중 부상 정보 갱신 (다른 ESPN 요청은 경기 뒤로 미룸)
CHANNELS = ("now", "last", "prev2")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def season_of(d: datetime) -> str:
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def prev_season(season: str) -> str:
    y = int(season[:4]) - 1
    return f"{y}-{(y + 1) % 100:02d}"


# 경기 ID 앞 세 자리 → 종류. 여기 있는 종류만 고른다 (올스타전 등은 제외)
GAME_TYPES = {"001": "프리시즌", "002": "정규시즌", "004": "플레이오프", "005": "플레이인", "006": "NBA컵 결승"}


def season_type(game_id: str) -> str:
    return GAME_TYPES.get(game_id[:3], "")


# ---------------------------------------------------------------- 일정

class Schedule:
    """팀 일정(이번 시즌 + 지난 시즌). 오늘 경기 상태는 라이브 스코어보드로 덮어쓴다."""

    def __init__(self, team: str):
        self.team = team
        self.games: list[dict] = []
        self.loaded_at: datetime | None = None
        cur = season_of(now_utc())
        for season in (cur, prev_season(cur)):
            path = os.path.join(CACHE, f"schedule_{season}.json")
            if os.path.exists(path):
                with open(path) as f:
                    self.games.extend(json.load(f)["games"])
        self.games.sort(key=lambda g: g["tipoff_utc"])

    def refresh(self, force: bool = False) -> None:
        """이번 시즌 + 지난 시즌 일정. 한 시즌이라도 받기에 실패하면 그 시즌은 마지막으로 성공한 값을 쓴다.
        (예전엔 이번 시즌만 실패하면 지난 시즌 일정만 남아 '지금 경기'가 비고 지난 경기가 플레이오프로 바뀌었다)"""
        every = SCHEDULE_REFRESH_NEAR if self.near_game(timedelta(hours=2), timedelta(hours=5)) else SCHEDULE_REFRESH
        if not force and self.loaded_at and now_utc() - self.loaded_at < every:
            self._overlay_live()
            return
        cur = season_of(now_utc())
        games, missing = [], []
        for season in (cur, prev_season(cur)):
            rows = self._season(season)
            if rows is None:
                missing.append(season)
                continue
            games.extend(rows)
        if cur in missing:
            # 이번 시즌 일정이 아예 없으면 화면을 바꾸지 않는다 (지난 시즌만으로 덮어쓰지 않는다)
            print(f"[일정] {cur} 일정이 없어 기존 일정을 유지합니다", flush=True)
        elif games:
            self.games = sorted(games, key=lambda g: g["tipoff_utc"])
            self.loaded_at = now_utc()
        self._overlay_live()

    def _season(self, season: str) -> list[dict] | None:
        """한 시즌 일정을 받아 디스크에 저장한다. 실패하면 마지막으로 저장된 일정을 돌려주고,
        resilient.py 가 30초·1분·5분 뒤 다시 받는다 (다시 받으면 바로 화면 갱신). 저장된 것도 없으면 None."""
        path = os.path.join(CACHE, f"schedule_{season}.json")
        if season != season_of(now_utc()) and os.path.exists(path):   # 끝난 시즌 일정은 바뀌지 않는다 → 다시 받지 않음
            with open(path) as f:
                return json.load(f)["games"]

        def fetch():
            from nba_api.stats.endpoints import scheduleleaguev2
            df = scheduleleaguev2.ScheduleLeagueV2(season=season, timeout=30).get_data_frames()[0]
            df = df[(df["homeTeam_teamTricode"] == self.team) | (df["awayTeam_teamTricode"] == self.team)]
            games = []
            for r in df.itertuples():
                if r.gameId[:3] not in GAME_TYPES:
                    continue
                sub = " ".join(x for x in (str(r.gameLabel or ""), str(r.gameSubLabel or "")) if x and x != "nan").strip()
                games.append({"label": sub, "game_id": r.gameId, "tipoff_utc": r.gameDateTimeUTC, "home": r.homeTeam_teamTricode,
                              "away": r.awayTeam_teamTricode, "arena": f"{r.arenaName}, {r.arenaCity}",
                              "status": int(r.gameStatus), "status_text": r.gameStatusText, "type": season_type(r.gameId)})
            return {"fetched_utc": now_utc().isoformat(timespec="seconds"), "games": games}

        def recovered():
            self.loaded_at = None   # 다음 바퀴(30초 안)에 새 일정으로 바꾼다
        key = f"schedule_team_{season}"
        try:
            saved = RS.fetch(key, fetch, path, label="일정", on_recover=recovered)
        except RS.Unavailable:
            print(f"[일정] {season} 받기 실패, 저장된 일정도 없음 → logs/fetch_errors.log", flush=True)
            return None
        if RS.failing(key):
            print(f"[일정] {season} 받기 실패 → {saved['fetched_utc']} 에 저장한 일정 사용 (자동 재시도)", flush=True)
        return saved["games"]

    def near_game(self, before: timedelta, after: timedelta) -> bool:
        """닉스 경기 팁오프 before 전 ~ after 뒤 안이거나 진행 중인 경기가 있나."""
        now = now_utc()
        return any(g["status"] == 2 or parse_utc(g["tipoff_utc"]) - before <= now <= parse_utc(g["tipoff_utc"]) + after
                   for g in self.games if g["status"] != 3 or now - parse_utc(g["tipoff_utc"]) < after)

    def _overlay_live(self) -> None:
        # 오늘 경기 상태(시작·종료)를 라이브 스코어보드로 빨리 반영. 예전엔 30초마다 늘 받았다(하루 2,880건) →
        # 닉스 경기 앞뒤(팁오프 15분 전 ~ 5시간 뒤)에만, 2분에 한 번, 라이브 추적 중이 아닐 때만
        if NH.is_live() or not self.near_game(timedelta(minutes=15), timedelta(hours=5)):
            return
        if time.time() - getattr(self, "overlay_at", 0) < 120:
            return
        self.overlay_at = time.time()
        try:
            from nba_api.live.nba.endpoints import scoreboard
            today = scoreboard.ScoreBoard(headers=T.BROWSER_HEADERS, timeout=15).get_dict()["scoreboard"]["games"]
        except Exception:
            return
        by_id = {g["game_id"]: g for g in self.games}
        for lg in today:
            g = by_id.get(lg["gameId"])
            if g:
                g["status"], g["status_text"] = int(lg["gameStatus"]), lg["gameStatusText"]

    def picks(self) -> dict[str, dict | None]:
        now = now_utc()
        live = [g for g in self.games if g["status"] == 2]
        upcoming = [g for g in self.games if g["status"] == 1 and parse_utc(g["tipoff_utc"]) >= now - timedelta(hours=4)]
        finals = sorted([g for g in self.games if g["status"] == 3], key=lambda g: g["tipoff_utc"], reverse=True)
        return {"now": (live or upcoming or [None])[0], "last": finals[0] if finals else None,
                "prev2": finals[1] if len(finals) > 1 else None}


# ---------------------------------------------------------------- 경기 전 예측

def predict(home: str, away: str, season: str) -> dict:
    """지난 시즌 넷레이팅 차이 + 홈 어드밴티지로 기대 점수 차를 잡고, 승률로 바꾼다."""
    path = os.path.join(CACHE, f"teamstats_{season}.json")
    if not os.path.exists(path):
        from nba_api.stats.endpoints import leaguedashteamstats
        from nba_api.stats.static import teams
        df = leaguedashteamstats.LeagueDashTeamStats(season=season, measure_type_detailed_defense="Advanced",
                                                     per_mode_detailed="PerGame", timeout=30).get_data_frames()[0]
        abbr = {t["id"]: t["abbreviation"] for t in teams.get_teams()}
        rows = {abbr[r.TEAM_ID]: {"net": float(r.NET_RATING), "pace": float(r.PACE), "w": int(r.W), "l": int(r.L)}
                for r in df.itertuples() if r.TEAM_ID in abbr}
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "w") as f:
            json.dump(rows, f)
    with open(path) as f:
        rows = json.load(f)
    h, a = rows.get(home), rows.get(away)
    if not h or not a:
        return {"error": "팀 기록이 없어 예측하지 못했습니다."}
    pace = (h["pace"] + a["pace"]) / 2
    margin = (h["net"] - a["net"]) * pace / 100 + T.WP_HOME_EDGE
    p = 0.5 * (1 + math.erf(margin / T.WP_SIGMA / math.sqrt(2)))
    return {"home": home, "away": away, "home_win": round(p, 3), "expected_margin": round(margin, 1),
            "basis": f"{season} 정규시즌 넷레이팅(100번 공격당 득실차) 차이 × 평균 페이스 + 홈 어드밴티지 {T.WP_HOME_EDGE}점, "
                     f"점수 차 표준편차 {T.WP_SIGMA}점으로 승률 환산",
            "teams": {home: h, away: a}, "season": season}


# ---------------------------------------------------------------- 다른 출처의 경기 전 예측 (ESPN)

ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"


def espn_pregame(game: dict) -> dict:
    """ESPN 매치업 예측(predictor)과 배당(pickcenter). 없는 항목은 None → 화면에 '제공 안 됨'."""
    import requests
    import briefing
    out = {"espn": None, "odds": None, "event_id": None, "errors": []}
    info = {"game_id": game["game_id"], "tipoff_utc": game["tipoff_utc"], "home": game["home"], "away": game["away"]}
    try:
        ev = _retry(lambda: briefing.espn_event(info))
    except Exception as exc:
        out["errors"].append(RS.friendly("ESPN 경기 정보", exc, game["game_id"]))
        return out
    if not ev:
        out["errors"].append("ESPN 일정에서 경기를 찾지 못함")
        return out
    out["event_id"] = ev["id"]
    hdr = {"User-Agent": "Mozilla/5.0"}
    try:
        sm = _retry(lambda: NH.SESSION.get(f"{ESPN_SITE}/summary", params={"event": ev["id"]}, headers=hdr, timeout=20).json())
    except Exception as exc:
        out["errors"].append(RS.friendly("ESPN 경기 요약", exc, game["game_id"]))
        return out
    # 1) 매치업 예측: summary.predictor → 없으면 core API predictor
    home_p = _predictor_home(sm.get("predictor"))
    if home_p is None:
        try:
            r = NH.SESSION.get(f"{ESPN_CORE}/events/{ev['id']}/competitions/{ev['id']}/predictor", headers=hdr, timeout=15)
            if r.ok:
                home_p = _predictor_home(r.json())
        except Exception:
            pass
    if home_p is not None:
        out["espn"] = {"home_win": round(home_p, 3), "source": "ESPN 매치업 예측 (BPI)"}
    # 2) 배당: 머니라인 → 내재 확률 → 수수료(vig) 제거
    for pc in sm.get("pickcenter") or []:
        h_ml = (pc.get("homeTeamOdds") or {}).get("moneyLine")
        a_ml = (pc.get("awayTeamOdds") or {}).get("moneyLine")
        if h_ml is None or a_ml is None:
            continue
        ph, pa = _implied(h_ml), _implied(a_ml)
        out["odds"] = {"home_win": round(ph / (ph + pa), 3), "home_ml": h_ml, "away_ml": a_ml,
                       "vig": round((ph + pa - 1) * 100, 1), "spread": pc.get("details"), "total": pc.get("overUnder"),
                       "source": f"{(pc.get('provider') or {}).get('name', '배당사')} 머니라인 (ESPN 제공)"}
        break
    return out


def _retry(fn, tries: int = 3, wait: float = 5.0):
    """네트워크가 잠깐 끊겼을 때(DNS 실패·타임아웃) 몇 초 간격으로 다시 시도."""
    for i in range(tries):
        try:
            return fn()
        except NH.NotSent:
            raise   # 요청 관리자가 보내지 않음 (쉬는 중·연결 없음) → 다시 시도하지 않는다
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(wait * (i + 1))


def _implied(ml: float) -> float:
    """미국식 머니라인 → 내재 확률 (수수료 포함)."""
    ml = float(ml)
    return -ml / (-ml + 100) if ml < 0 else 100 / (ml + 100)


def _predictor_home(pred: dict | None) -> float | None:
    """ESPN predictor 의 홈 승리 확률(0~1). 형식이 두 가지라 둘 다 본다."""
    if not pred:
        return None
    t = pred.get("homeTeam") or {}
    for key in ("gameProjection", "teamChance"):
        if t.get(key) not in (None, ""):
            return float(t[key]) / 100
    for st in t.get("statistics", []):
        if st.get("name") in ("gameProjection", "teamChance"):
            return float(st.get("value")) / 100
    return None


# ---------------------------------------------------------------- 채널

class NowChannel:
    """지금 경기: 프리뷰 → (팁오프 1시간 전) 라이브 추적."""

    def __init__(self, game: dict, team: str, villains: R.VillainService | None = None):
        self.game, self.team = game, team
        self.villains = villains
        self.tracker: T.Tracker | None = None
        self.final_seen: datetime | None = None
        log_dir = os.path.join(T.LOG_ROOT, "app", f"now-{game['game_id']}")
        os.makedirs(log_dir, exist_ok=True)
        self.brief = T.BriefingManager(game["game_id"], [3600, 300], log_dir, None)
        self.lineups = LU.LineupService(game, team, lambda: (self.brief.data or {}).get("injuries"))
        self.brief.expected_hook = self.lineups.expected_names
        self.brief_seen: str | None = None
        self.created = now_utc()
        opp = game["away"] if game["home"] == team else game["home"]
        self.opp = opp
        if villains:
            villains.get(opp, team)   # 빌런 카드 계산을 미리 시작
        self.h2h = None
        self.prediction = None
        self.more = None              # ESPN 예측·배당
        self.more_at: datetime | None = None
        self.inj_at: datetime | None = None
        self.inj_busy = False
        threading.Thread(target=self._load_preview, args=(opp,), daemon=True).start()

    def _load_preview(self, opp: str) -> None:
        try:
            self.h2h = T.head_to_head(self.team, opp)
        except Exception as exc:
            self.h2h = {"games": [], "note": RS.friendly("맞대결 기록", exc, opp)}
        try:
            self.prediction = predict(self.game["home"], self.game["away"], prev_season(season_of(parse_utc(self.game["tipoff_utc"]))))
        except Exception as exc:
            self.prediction = {"error": RS.friendly("예측용 기록", exc)}
        self._load_more()

    def _load_more(self) -> None:
        try:
            self.more = espn_pregame(self.game)
        except Exception as exc:
            self.more = {"espn": None, "odds": None, "errors": [RS.friendly("ESPN 예측·배당", exc)]}
        self.more_at = now_utc()

    def tick(self) -> None:
        # 배당은 경기 전까지 바뀌므로 30분마다 다시 받는다. 받기 실패했으면 3분 뒤 다시
        failed = bool(self.more and self.more.get("errors") and not (self.more.get("espn") or self.more.get("odds")))
        every = timedelta(minutes=3) if failed else timedelta(minutes=30)
        if self.more_at and now_utc() - self.more_at > every and self.game["status"] == 1:
            self.more_at = now_utc()
            threading.Thread(target=self._load_more, daemon=True).start()
        self.brief.tick({"tipoff_utc": self.game["tipoff_utc"]})
        # 선발 예측: 부상자 정보(브리핑)가 들어온 뒤 계산, 브리핑이 갱신되면 다시
        bd = self.brief.data
        if bd or now_utc() - self.created > timedelta(seconds=90):
            if bd and self.lineups.data and bd.get("refreshed_utc") != self.brief_seen:
                self.lineups._rebuild("부상자·뉴스 갱신 반영")
            self.brief_seen = (bd or {}).get("refreshed_utc")
            self.lineups.tick()
        tip = parse_utc(self.game["tipoff_utc"])
        if self.tracker is None and (now_utc() >= tip - LIVE_START_BEFORE or self.game["status"] == 2):
            print(f"[지금 경기] 라이브 추적 시작: {self.game['away']} @ {self.game['home']}", flush=True)
            tr = T.Tracker(T.AutoSource(self.game["game_id"], "auto", self.game), self.game["game_id"], 30,
                           f"app/live-{self.game['game_id']}", "live")
            tr.notify, tr.brief = True, self.brief
            tr.delay_sec = T.load_delay()
            # 빌런 알림 + 닉스 실책 몰림 알림 (같은 Mac 알림 경로)
            tr.alert_hook = TV.Hooks(R.VillainWatch(self.villains, self.opp, self.team) if self.villains else None,
                                     TV.TurnoverWatch(self.team))
            self.tracker = tr
            threading.Thread(target=tr.run, daemon=True).start()
        if self.tracker and self.tracker.state.get("status") == "live":
            self._live_injuries()
        if self.tracker and self.tracker.state.get("status") == "final" and self.final_seen is None:
            self.final_seen = now_utc()

    def _live_injuries(self) -> None:
        """경기 중 부상 정보 5분마다. 라이브 추적이 방금 받은 ESPN 경기 요약에 들어 있으면 그걸 쓰고(요청 0건),
        아니면(cdn 으로 추적 중) 경기 요약을 한 번 받는다."""
        if self.inj_busy or (self.inj_at and now_utc() - self.inj_at < INJURY_LIVE_EVERY):
            return
        esp = getattr(self.tracker.source, "espn", None)
        ev_id = (esp.event_id if esp else None) or ((self.brief.data or {}).get("espn_event") or {}).get("id")
        if not ev_id:
            return
        self.inj_busy, self.inj_at = True, now_utc()

        def run():
            import briefing
            try:
                fresh = esp and esp.last_summary and time.time() - esp.last_summary_at < 60
                inj = briefing.injuries(ev_id, esp.last_summary if fresh else None)
                with self.brief.lock:
                    if self.brief.data is not None:
                        self.brief.data = {**self.brief.data, "injuries": inj,
                                           "injuries_live_utc": now_utc().isoformat(timespec="seconds")}
            except Exception as exc:
                RS.log_error("경기 중 부상 정보", exc, self.game["game_id"])
            finally:
                self.inj_busy = False
        threading.Thread(target=run, daemon=True).start()

    def done(self) -> bool:
        return self.final_seen is not None and now_utc() - self.final_seen >= NEXT_GAME_AFTER_FINAL

    def snapshot(self) -> dict:
        if self.tracker and self.tracker.state.get("status") in ("live", "final"):
            return {"kind": "live", **self.tracker.snapshot()}
        return {"kind": "preview", "briefing": self.brief.snapshot(), "h2h": self.h2h,
                "prediction": self.prediction, "prediction_more": self.more,
                "live_start_utc": (parse_utc(self.game["tipoff_utc"]) - LIVE_START_BEFORE).isoformat(),
                "tracking": bool(self.tracker),
                "state": {"status": "preview", "home": self.game["home"], "away": self.game["away"],
                          "focus": self.team, "opponent": self.game["away"] if self.game["home"] == self.team else self.game["home"],
                          "tipoff_utc": self.game["tipoff_utc"], "alerts": [], "players": [], "teams": {}},
                "meta": {"mode": "preview"}}


def past_brief(game: dict, team: str, on_recover=None) -> dict:
    """지난 경기의 '재생 전 브리핑' — 결과가 드러나지 않는 정보만 (선발, 뛰지 않은 선수, 경기 전 예측, 그 이전 맞대결)."""
    from nba_api.live.nba.endpoints import boxscore
    from nba_api.stats.endpoints import boxscoretraditionalv3
    gid = game["game_id"]
    d = os.path.join(T.REPLAY_DATA, gid)
    os.makedirs(d, exist_ok=True)

    def cached(name, fetch):
        # 없거나 깨진 파일(예전에 받기 실패로 남은 빈 파일)이면 다시 받는다. 성공했을 때만 저장
        path = os.path.join(d, name)
        data = RS._load(path)
        return data if data is not None else RS.fetch(f"box_{gid}_{name}", fetch, path, label="박스스코어",
                                                      on_recover=on_recover)
    out = {"teams": {}, "errors": []}
    try:
        try:
            live = cached("live_boxscore.json", lambda: boxscore.BoxScore(gid, headers=T.BROWSER_HEADERS, timeout=30).get_dict()["game"])
        except Exception as exc:   # NBA 가 막혔으면 ESPN 경기 요약 (선발·결장 사유 포함)
            import espn_source
            live = T._cached_json(os.path.join(d, "espn_boxscore.json"), lambda: espn_source.final_game(gid, game)["box"])
            out["source"] = "ESPN"
            RS.log_error("박스스코어", exc, gid, "NBA 대신 ESPN 경기 요약 사용")
        try:
            v3 = cached("box_v3.json", lambda: boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=gid, timeout=30).get_dict())["boxScoreTraditional"]
            comments = {p["personId"]: p.get("comment", "") for side in ("homeTeam", "awayTeam") for p in v3[side]["players"]}
        except Exception:
            if out.get("source") != "ESPN":
                raise
            comments = {p["personId"]: p.get("comment", "") for side in ("homeTeam", "awayTeam") for p in live[side]["players"]}
        for side in ("awayTeam", "homeTeam"):
            t = live[side]
            out["teams"][t["teamTricode"]] = {
                # 점수·기록은 싣지 않는다
                "starters": [{"name": p["nameI"], "pos": p.get("position", "")} for p in t["players"] if p.get("starter") == "1"],
                "dnp": [{"name": p["nameI"], "why": _dnp_ko(comments.get(p["personId"], ""))}
                        for p in t["players"] if p.get("played") == "0"],
            }
        if live.get("arena"):
            out["arena"] = f"{live['arena']['arenaName']}, {live['arena']['arenaCity']}"
    except Exception as exc:
        out["errors"].append(RS.friendly("박스스코어", exc, gid))
    opp = game["away"] if game["home"] == team else game["home"]
    tip = parse_utc(game["tipoff_utc"])
    try:
        out["prediction"] = predict(game["home"], game["away"], prev_season(season_of(tip)))
    except Exception as exc:
        out["prediction"] = {"error": RS.friendly("예측용 기록", exc, gid)}
    try:
        out["h2h"] = T.head_to_head(team, opp, before=(tip - timedelta(hours=10)).date().isoformat())
    except Exception as exc:
        out["h2h"] = {"games": [], "note": RS.friendly("맞대결 기록", exc, gid)}
    try:
        out["prediction_more"] = espn_pregame(game)
    except Exception as exc:
        out["prediction_more"] = {"espn": None, "odds": None, "errors": [RS.friendly("ESPN 예측·배당", exc, gid)]}
    out["box_link"] = f"https://www.nba.com/game/{gid}"
    return out


def _dnp_ko(comment: str) -> str:
    c = comment.replace("DNP - ", "").replace("DND - ", "").replace("NWT - ", "")
    return {"Coach's Decision": "감독 결정", "": "출전 기록 없음"}.get(c, c.replace("Injury/Illness", "부상/질병"))


class ReplayChannel:
    """지난/전전 경기: 먼저 그 경기 브리핑, 재생 버튼을 누르면 리플레이."""

    def __init__(self, game: dict, name: str, team: str = T.FOCUS):
        self.game, self.name = game, name
        self.tracker: T.Tracker | None = None
        self.loading = False
        self.error: str | None = None
        self.pre: dict | None = None
        self.team = team
        self.pre_loading = False
        self.pre_at = 0.0
        self.pre_fails = 0
        self._load_pre()

    def _load_pre(self) -> None:
        """재생 전 브리핑. 일부를 못 받았으면 (경기 직후 stats.nba.com 기록이 아직 비어 있는 경우 등)
        다시 받았을 때(on_recover) 또는 30초·1분·5분 뒤(tick) 다시 계산한다. 예전엔 채널이 생길 때 한 번만 계산해서
        나중에 데이터가 들어와도 '받지 못했습니다' 가 계속 남았다."""
        if self.pre_loading:
            return
        self.pre_loading = True

        def load():
            try:
                pre = past_brief(self.game, self.team, on_recover=self._load_pre)
                self.pre_fails = self.pre_fails + 1 if pre.get("errors") else 0
                self.pre = pre
            finally:
                self.pre_at, self.pre_loading = time.time(), False
        threading.Thread(target=load, daemon=True).start()

    def tick(self) -> None:
        if self.pre and self.pre.get("errors") and not self.pre_loading:
            wait = RS.RETRY[min(self.pre_fails, len(RS.RETRY)) - 1] if self.pre_fails else 0
            if time.time() - self.pre_at >= wait:
                self._load_pre()

    def control(self, cmd: dict) -> dict:
        action = cmd.get("action")
        if action in ("start", "restart"):
            if self.loading:
                return {"loading": True}
            if self.tracker:
                self.tracker.stopped = True
            self.tracker, self.loading, self.error = None, True, None
            speed = float(cmd.get("value") or 10)

            def start():
                try:
                    src = T.ReplaySource(self.game["game_id"], speed, 3, "live")
                    tr = T.Tracker(src, self.game["game_id"], 3, f"app/replay-{self.game['game_id']}", "replay")
                    tr.notify = False
                    threading.Thread(target=tr.run, daemon=True).start()
                    self.tracker = tr
                except Exception as exc:
                    self.error = RS.friendly("리플레이 데이터", exc, self.game["game_id"], "재생 버튼을 다시 눌러 주세요")
                finally:
                    self.loading = False
            threading.Thread(target=start, daemon=True).start()
            return {"loading": True}
        if not self.tracker:
            return {"error": "먼저 재생 버튼을 눌러 주세요"}
        return self.tracker.control(cmd)

    def snapshot(self) -> dict:
        if self.tracker:
            return {"kind": "replay", **self.tracker.snapshot()}
        return {"kind": "replay-ready", "loading": self.loading, "error": self.error, "pre_brief": self.pre,
                "state": {"status": "ready", "home": self.game["home"], "away": self.game["away"],
                          "focus": T.FOCUS, "alerts": [], "players": [], "teams": {}},
                "meta": {"mode": "replay"}}


# ---------------------------------------------------------------- 관리자

class App:
    def __init__(self, team: str):
        self.team = team
        self.schedule = Schedule(team)
        self.villains = R.VillainService()
        # 닉스 경기 시간(팁오프 1시간 전 ~ 4시간 뒤): 리그 전 경기 데이터 같은 무거운 갱신을 하지 않는다
        NH.set_busy_fn(lambda: self.schedule.near_game(timedelta(hours=1), timedelta(hours=4)))
        self.league = LG.LeagueDesk(self._next_opp)
        self.desk = DK.Desk(self.league, self._next_opp)
        self.channels: dict[str, object | None] = dict.fromkeys(CHANNELS)
        self.games: dict[str, dict | None] = dict.fromkeys(CHANNELS)
        self.lock = threading.Lock()
        self.last_error: str | None = None
        self.bg: dict[str, dict] = {}   # 경기별 백그라운드 계산 (경기 후 분석·일정 지표): {"at", "busy", "data", "error"}

    def _bg_get(self, key: str, fn, max_age: float, keys_for_retry=()) -> dict:
        """무거운 계산을 백그라운드에서 돌리고, 지금 상태를 바로 돌려준다 (화면 요청이 기다리지 않게).
        실패하면 화면용 이유와, resilient.py 가 다시 받는 중인 데이터의 다음 재시도 시각을 함께 준다."""
        job = self.bg.setdefault(key, {"at": 0.0, "busy": False, "data": None, "error": None})
        fresh = job["data"] is not None and time.time() - job["at"] < max_age
        retry_due = job["error"] and time.time() - job["at"] >= RS.RETRY[0]
        first = job["data"] is None and (not job["error"] or retry_due)   # 아직 없음 (실패했으면 30초 뒤 다시)
        expired = job["data"] is not None and not fresh                    # 있지만 오래됨 (실패해도 있던 것을 보여 줌)
        if not job["busy"] and (first or expired):
            job["busy"] = True

            def run():
                try:
                    job["data"], job["error"] = fn(), None
                except Exception as exc:
                    job["error"] = RS.friendly("경기 후 분석" if key.startswith("pg") else "일정", exc, key, "30초 뒤 다시 시도")
                finally:
                    job["at"], job["busy"] = time.time(), False
            threading.Thread(target=run, daemon=True).start()
        if job["data"] is not None:
            return {"status": "ready", **job["data"]}
        retry = [n for n in RS.notices() if any(k in n["key"] for k in keys_for_retry)]
        if job["error"]:
            return {"status": "error", "error": job["error"], "retry": retry,
                    "next_utc": datetime.fromtimestamp(job["at"] + RS.RETRY[0], timezone.utc).isoformat(timespec="seconds")}
        return {"status": "loading", "retry": retry}

    def _team_games(self, g: dict) -> int:
        """이 경기까지 닉스가 이번 시즌 치른 정규시즌 경기 수 (경기 후 분석의 기준값 고르기용)."""
        season = season_of(parse_utc(g["tipoff_utc"]))
        return sum(1 for x in self.schedule.games if x["game_id"][:3] == "002" and x["status"] == 3
                   and season_of(parse_utc(x["tipoff_utc"])) == season and x["tipoff_utc"] <= g["tipoff_utc"])

    def analysis(self, ch: str) -> dict:
        """경기 후 분석 (결과가 드러나므로 화면에서 펼쳤을 때만 부른다)."""
        g = self.games.get(ch)
        if ch not in ("last", "prev2") or not g:
            return {"status": "error", "error": "다시 보기 화면에서만 볼 수 있습니다"}
        if g["status"] != 3:
            return {"status": "error", "error": "경기가 아직 끝나지 않았습니다"}
        gid = g["game_id"]
        return self._bg_get(f"pg_{gid}", lambda: PG.build(gid, self.team, self._team_games(g)), 6 * 3600,
                            (f"pg_{gid}", "league_totals", "league_misc"))

    def fatigue(self, g: dict) -> dict:
        return self._bg_get(f"ft_{g['game_id']}", lambda: FT.build(g["game_id"], self.team, g["tipoff_utc"]), 600,
                            ("schedule_full",))

    def loop(self) -> None:
        """두 갈래로 돈다: 일정 갱신(stats.nba.com 이 느리면 1~2분 걸릴 수 있음)과,
        채널 맞추기·라이브 추적 시작(10초마다). 추적 시작이 일정 갱신을 기다리지 않는다."""
        threading.Thread(target=self._schedule_loop, daemon=True).start()
        while True:
            try:
                now_ch = self.channels.get("now")
                self._sync(self.schedule.picks(), force_now=isinstance(now_ch, NowChannel) and now_ch.done())
                if isinstance(self.channels.get("now"), NowChannel):
                    self.channels["now"].tick()
                for ch in ("last", "prev2"):
                    if isinstance(self.channels.get(ch), ReplayChannel):
                        self.channels[ch].tick()
                self.league.tick()
                self.desk.tick()
            except Exception as exc:
                self.last_error = RS.friendly("서버 내부 갱신", exc, then="10초 뒤 다시 시도")
                traceback.print_exc()
            time.sleep(10)

    def _next_opp(self) -> str | None:
        g = self.games.get("now")
        if not g:
            return None
        return g["away"] if g["home"] == self.team else g["home"]

    def _schedule_loop(self) -> None:
        while True:
            try:
                now_ch = self.channels.get("now")
                self.schedule.refresh(force=isinstance(now_ch, NowChannel) and now_ch.done())
                self.last_error = None
            except Exception as exc:
                self.last_error = RS.friendly("일정", exc, then="30초 뒤 다시 시도")
                traceback.print_exc()
            time.sleep(TICK_SEC)

    def _sync(self, picks: dict, force_now: bool) -> None:
        with self.lock:
            for ch in CHANNELS:
                g = picks.get(ch)
                cur = self.games.get(ch)
                if g is None:
                    self.games[ch], self.channels[ch] = None, None
                    continue
                if cur and cur["game_id"] == g["game_id"] and not (ch == "now" and force_now):
                    cur.update(status=g["status"], status_text=g["status_text"])
                    continue
                print(f"[채널] {ch}: {g['away']} @ {g['home']} ({g['game_id']}, {g['tipoff_utc']})", flush=True)
                self.games[ch] = dict(g)
                self.channels[ch] = NowChannel(self.games[ch], self.team, self.villains) if ch == "now" else ReplayChannel(self.games[ch], ch, self.team)

    def nav(self) -> dict:
        out = {}
        for ch in CHANNELS:
            g = self.games.get(ch)
            if not g:
                out[ch] = None
                continue
            opp = g["away"] if g["home"] == self.team else g["home"]
            status = "LIVE" if g["status"] == 2 else "예정" if g["status"] == 1 else "종료"
            # 지난 경기는 결과(점수)를 싣지 않는다 — 놓친 경기 스포일러 방지
            out[ch] = {"game_id": g["game_id"], "tipoff_utc": g["tipoff_utc"], "opp": opp,
                       "home": g["home"] == self.team, "status": status, "type": g["type"], "arena": g["arena"],
                       "label": g.get("label", "")}
        return out

    def view(self, ch: str) -> dict:
        c = self.channels.get(ch)
        # 화면 코드가 바뀌면 열려 있는 탭이 스스로 새로고침하도록 버전을 함께 보낸다
        ui_version = int(os.path.getmtime(os.path.join(T.WEB, "index.html")))
        base = {"channel": ch, "nav": self.nav(), "game": self.nav().get(ch), "server_error": self.last_error,
                "ui_version": ui_version, "data_status": RS.notices(),   # 지금 실패 중인 외부 데이터 (원문 없음)
                "net": NH.status()}   # 사이트별 정상 / 쉬는 중 / 대체 소스 사용 중 · 인터넷 연결 · 오늘 요청 수
        if ch == "league":
            return {**base, **self.league.snapshot(), **self.desk.snapshot(),
                    "state": {"status": "league", "home": "", "away": "", "focus": self.team, "alerts": [], "players": [], "teams": {}},
                    "meta": {"mode": "league"}}
        if c is None:
            return {**base, "kind": "empty"}
        out = {**base, **c.snapshot()}
        g = self.games.get(ch)
        if out["kind"] in ("live", "replay"):
            out["reactions"] = R.reactions_for(g["game_id"])
        if out["kind"] in ("preview", "replay-ready"):   # 일정·체력 (지난 경기는 '당시 일정 조건', 결과 없음)
            out["fatigue"] = self.fatigue(g)
        if isinstance(c, NowChannel) and out["kind"] in ("preview", "live"):
            out["villains"] = self.villains.get(c.opp, self.team)
            out["lineups"] = c.lineups.snapshot()
            out["roy_in_game"] = self.league.roy_in_game(g["home"], g["away"])
            out["rival_notes"] = R.rivals_notes()
            out["rival_tags"] = R.TAG_PRESETS
        return out

    def react(self, ch: str, body: dict) -> dict:
        """한 줄 반응 저장 — 쿼터·시간·점수는 그 채널 화면의 지금 값으로 기록."""
        c, g = self.channels.get(ch), self.games.get(ch)
        tr = getattr(c, "tracker", None)
        if not tr or not g:
            return {"error": "경기 중에만 기록할 수 있습니다"}
        with tr.lock:
            state = dict(tr.view_state())
        mode = "replay" if isinstance(c, ReplayChannel) else "live"
        try:
            return {"ok": True, "reaction": R.add_reaction(state, g["game_id"], mode, body.get("text", ""), body.get("emoji", ""))}
        except ValueError as exc:
            return {"error": str(exc)}

    def replay_marks(self, ch: str) -> dict:
        """리플레이 하이라이트 표시(런·역전·최대 리드). 경기 전체를 보므로 화면에서 토글을 켰을 때만 부른다."""
        c = self.channels.get(ch)
        tr = getattr(c, "tracker", None)
        src = getattr(tr, "source", None)
        if not src or not hasattr(src, "actions"):
            return {"error": "리플레이 중이 아닙니다"}
        home, away = src.box["homeTeam"]["teamTricode"], src.box["awayTeam"]["teamTricode"]
        return T.replay_marks(src.actions, home, away)

    def set_delay(self, value) -> dict:
        """중계 딜레이 설정 (지금 경기의 화면·Mac 알림에 바로 적용, 다음 경기에도 유지)."""
        try:
            v = T.save_delay(value)
        except (TypeError, ValueError):
            return {"error": "숫자를 넣어 주세요"}
        c = self.channels.get("now")
        if isinstance(c, NowChannel) and c.tracker:
            c.tracker.delay_sec = v
        print(f"  ■ 중계 딜레이: {v:g}초", flush=True)
        return {"ok": True, "delay_sec": v}

    def control(self, ch: str, cmd: dict) -> dict:
        c = self.channels.get(ch)
        if isinstance(c, ReplayChannel):
            return c.control(cmd)
        return {"error": "이 화면에서는 재생 제어를 할 수 없습니다"}


def serve(app: App, port: int, open_browser: bool) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            ch = (q.get("ch") or ["now"])[0]
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(T.WEB, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/games":
                    return self._json(app.nav())
                if u.path == "/api/view" and (ch in CHANNELS or ch == "league"):
                    return self._json(app.view(ch))
                if u.path == "/api/analysis" and ch in CHANNELS:
                    return self._json(app.analysis(ch))
                if u.path == "/api/replay_marks" and ch in CHANNELS:
                    return self._json(app.replay_marks(ch))
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
            self._send(404, b"", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            ch = (parse_qs(u.query).get("ch") or [""])[0]
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if u.path == "/api/delay":
                    return self._json(app.set_delay(body.get("value")))
                if u.path == "/api/control" and ch in CHANNELS:
                    return self._json(app.control(ch, body))
                if u.path == "/api/reaction" and ch in CHANNELS:
                    return self._json(app.react(ch, body))
                if u.path == "/api/reaction/delete":
                    return self._json({"ok": R.delete_reaction(str(body.get("id", "")))})
                if u.path == "/api/pick":
                    b = app.league.board
                    c = next((x for x in (b or {}).get("cands", []) if x["name"] == body.get("name")), None)
                    if not c:
                        return self._json({"error": "신인왕 후보 목록에 없는 선수입니다"}, 400)
                    LG.set_pick(LG.season(), c["name"], c["team"], c["id"])
                    LG._track_pick(LG.season(), b["cands"])   # 고른 날의 순위부터 기록
                    return self._json({"ok": True, "pick": LG.picks()["seasons"][LG.season()]})
                if u.path == "/api/rival":
                    note = R.save_rival_note(int(body["player_id"]), body.get("name", ""), body.get("team", ""),
                                             body.get("memo", ""), body.get("tags") or [])
                    return self._json({"ok": True, "note": note})
            except Exception as exc:
                return self._json({"error": str(exc)}, 400)
            self._send(404, b"", "text/plain")

    threading.Thread(target=app.loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    host, ip = T.local_hostname(), T.lan_ip()
    print(f"대시보드: http://127.0.0.1:{port}/")
    print(f"폰(같은 와이파이): http://{host}.local:{port}/" + (f"  (IP: http://{ip}:{port}/)" if ip else ""))
    print("한 번 켜 두면 계속 돕니다. 끄려면 Ctrl+C", flush=True)
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team", default=T.FOCUS)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    serve(App(args.team), args.port, not args.no_open)


if __name__ == "__main__":
    main()
