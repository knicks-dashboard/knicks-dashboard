"""리그 데스크: 신인왕 레이스 · 리그 소식 · 동/서부 순위.

- 신인 기록: nba_api LeagueDashPlayerStats(player_experience_nullable="Rookie")
- 신인왕 후보: ESPN 퓨처스(DraftKings) 신인왕 배당 상위 6명, 주 1회 스냅숏 저장 (+ 웹 검색으로 찾은 교차 확인 자료: league/roy_sources.json)
- 나의 신인왕 픽: test_live/my_picks.json
- 리그 소식: 매일 07:00(한국) 한 번 '재미 기준' 5개 선정 → league/news_<날짜>.json
모든 쓰기는 test_live/ 안에서만.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import briefing
import resilient as RS
import nba_http as NH
import rivals as RV
import tracker as T

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "league")
PICKS_PATH = os.path.join(HERE, "my_picks.json")
H = {"User-Agent": "Mozilla/5.0"}
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
ROY_FUTURE = 2582
ROY_TOP = 6
PAST_ROY = [("2025-26", 1642843, "Cooper Flagg", "DAL"), ("2024-25", 1642264, "Stephon Castle", "SAS"),
            ("2023-24", 1641705, "Victor Wembanyama", "SAS")]
NEWS_HOUR_KST = 7
NEWS_PICK = 5
WEST = {"DAL", "DEN", "GSW", "HOU", "LAC", "LAL", "MEM", "MIN", "NOP", "OKC", "PHX", "POR", "SAC", "SAS", "UTA"}
ESPN_TO_TRI = {v: k for k, v in briefing.ESPN_IDS.items()}
TEAM_KO = {"ATL": "애틀랜타", "BOS": "보스턴", "BKN": "브루클린", "CHA": "샬럿", "CHI": "시카고", "CLE": "클리블랜드", "DAL": "댈러스",
           "DEN": "덴버", "DET": "디트로이트", "GSW": "골든스테이트", "HOU": "휴스턴", "IND": "인디애나", "LAC": "LA 클리퍼스",
           "LAL": "LA 레이커스", "MEM": "멤피스", "MIA": "마이애미", "MIL": "밀워키", "MIN": "미네소타", "NOP": "뉴올리언스",
           "NYK": "뉴욕", "OKC": "오클라호마시티", "ORL": "올랜도", "PHI": "필라델피아", "PHX": "피닉스", "POR": "포틀랜드",
           "SAC": "새크라멘토", "SAS": "샌안토니오", "TOR": "토론토", "UTA": "유타", "WAS": "워싱턴"}


def season() -> str:
    """테스트용으로 LEAGUE_SEASON=2025-26 처럼 지난 시즌을 넣어 볼 수 있다."""
    return os.environ.get("LEAGUE_SEASON") or RV._season_of(datetime.now(timezone.utc))


def _path(name: str) -> str:
    os.makedirs(DIR, exist_ok=True)
    return os.path.join(DIR, name)


def _cached(name: str, fetch, max_age: timedelta | None):
    path = _path(name)
    if os.path.exists(path) and (max_age is None or time.time() - os.path.getmtime(path) < max_age.total_seconds()):
        with open(path) as f:
            return json.load(f)
    return RS.fetch(name, fetch, path)   # 실패하면 저장본 + 자동 재시도 (resilient.py)


def _iso_week(d: datetime | None = None) -> str:
    y, w, _ = (d or datetime.now(T.KST)).isocalendar()
    return f"{y}-W{w:02d}"


def _team_tri(team_id: int) -> str:
    from nba_api.stats.static import teams
    return next(t["abbreviation"] for t in teams.get_teams() if t["id"] == int(team_id))


def ko_name(name: str) -> str:
    """표준 한글 표기 사전(briefing/names_ko.json)에 있으면 한글, 없으면 영어 그대로 (멋대로 음역하지 않는다)."""
    full = _glossary().get("full", {})
    if name in full:
        return full[name]
    key = _norm(name)
    return next((v for k, v in full.items() if _norm(k) == key), name)


def _glossary() -> dict:
    with open(briefing.NAMES_KO) as f:
        return json.load(f)


# ---------------------------------------------------------------- 신인 기록

def rookie_stats(s: str) -> dict[int, dict]:
    def fetch():
        from nba_api.stats.endpoints import leaguedashplayerstats
        df = RV._stats_call(lambda: leaguedashplayerstats.LeagueDashPlayerStats(
            season=s, per_mode_detailed="PerGame", player_experience_nullable="Rookie", timeout=30).get_data_frames()[0])
        out = {}
        for r in df.itertuples():
            tsa = 2 * (r.FGA + 0.44 * r.FTA)
            out[str(int(r.PLAYER_ID))] = {"name": r.PLAYER_NAME, "team": r.TEAM_ABBREVIATION, "gp": int(r.GP),
                                          "pts": round(r.PTS, 1), "reb": round(r.REB, 1), "ast": round(r.AST, 1),
                                          "ts": round(r.PTS / tsa, 3) if tsa else None, "min": round(r.MIN, 1)}
        return out
    return {int(k): v for k, v in _cached(f"rookies_{s}.json", fetch, timedelta(hours=3)).items()}


def all_players(s: str) -> list[dict]:
    def fetch():
        from nba_api.stats.endpoints import commonallplayers
        df = RV._stats_call(lambda: commonallplayers.CommonAllPlayers(season=s, is_only_current_season=1, timeout=30).get_data_frames()[0])
        return [{"id": int(r.PERSON_ID), "name": r.DISPLAY_FIRST_LAST, "team": r.TEAM_ABBREVIATION or ""} for r in df.itertuples()]
    return _cached(f"players_{s}.json", fetch, timedelta(hours=12))


def standings(s: str) -> dict:
    def fetch():
        from nba_api.stats.endpoints import leaguestandingsv3
        df = RV._stats_call(lambda: leaguestandingsv3.LeagueStandingsV3(season=s, timeout=30).get_data_frames()[0])
        rows = []
        for r in df.itertuples():
            rows.append({"team": _team_tri(r.TeamID), "conf": r.Conference, "rank": int(r.PlayoffRank), "w": int(r.WINS),
                         "l": int(r.LOSSES), "pct": float(r.WinPCT), "gb": r.ConferenceGamesBack, "streak": r.strCurrentStreak,
                         "streak_n": int(r.CurrentStreak), "l10": r.L10})
        return rows
    rows = _cached(f"standings_{s}.json", fetch, timedelta(hours=2))
    label = f"{s} 정규시즌"
    if not any(r["w"] + r["l"] for r in rows):   # 개막 전이면 지난 시즌 최종 순위
        prev = RV._prev(s)
        rows = _cached(f"standings_{prev}.json", lambda: _standings_rows(prev), None)
        label = f"{prev} 최종 (새 시즌 개막 전)"
    out = {"label": label, "source": {"name": "NBA.com 순위 (stats.nba.com)", "url": "https://www.nba.com/standings"}}
    for conf in ("East", "West"):
        out[conf] = sorted([r for r in rows if r["conf"] == conf], key=lambda r: r["rank"])
    return out


def _standings_rows(s: str) -> list[dict]:
    from nba_api.stats.endpoints import leaguestandingsv3
    df = RV._stats_call(lambda: leaguestandingsv3.LeagueStandingsV3(season=s, timeout=30).get_data_frames()[0])
    return [{"team": _team_tri(r.TeamID), "conf": r.Conference, "rank": int(r.PlayoffRank), "w": int(r.WINS), "l": int(r.LOSSES),
             "pct": float(r.WinPCT), "gb": r.ConferenceGamesBack, "streak": r.strCurrentStreak, "streak_n": int(r.CurrentStreak),
             "l10": r.L10} for r in df.itertuples()]


# ---------------------------------------------------------------- 신인왕 배당 (주 1회)

def _implied(odds: str) -> float | None:
    try:
        v = float(str(odds).replace("+", ""))
    except ValueError:
        return None
    return 100 / (v + 100) if v > 0 else -v / (-v + 100)


def fetch_roy_odds(s: str) -> dict:
    year = int(s[:4]) + 1
    d = NH.SESSION.get(f"{ESPN_CORE}/seasons/{year}/futures/{ROY_FUTURE}", headers=H, timeout=20).json()
    fut = d["futures"][0]
    books = sorted(fut["books"], key=lambda b: _implied(b["value"]) or 0, reverse=True)[:ROY_TOP + 4]
    rows = []
    for b in books:
        a = NH.SESSION.get(b["athlete"]["$ref"].replace("http://", "https://"), headers=H, timeout=20).json()
        team_id = int(re.search(r"/teams/(\d+)", (a.get("team") or {}).get("$ref", "/teams/0")).group(1))
        rows.append({"name": a.get("displayName"), "espn_id": a.get("id"), "team": ESPN_TO_TRI.get(team_id, ""),
                     "odds": b["value"], "implied": round(_implied(b["value"]) or 0, 4)})
        if len(rows) >= ROY_TOP:
            break
    # 배당의 '마진'을 빼고 후보 6명 안에서 다시 나눈 확률
    tot = sum(r["implied"] for r in rows) or 1
    for r in rows:
        r["share"] = round(r["implied"] / tot, 4)
    return {"provider": f"{fut['provider']['name']} (ESPN 퓨처스)", "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "url": "https://www.espn.com/nba/futures", "rows": rows}


def roy_odds(s: str) -> dict:
    """주 1회 스냅숏. 이번 주(월~일) 스냅숏이 없을 때만 새로 받는다."""
    path = _path(f"roy_odds_{s}.json")
    data = RV._read(path, {"weeks": {}})
    wk = _iso_week()
    if wk not in data["weeks"]:
        try:
            data["weeks"][wk] = fetch_roy_odds(s)
            RV._write(path, data)
        except Exception as exc:
            if not data["weeks"]:
                raise
            print(f"[리그] 배당 갱신 실패, 지난 스냅숏 사용: {exc}", flush=True)
    weeks = sorted(data["weeks"])
    cur = data["weeks"][weeks[-1]]
    prev = data["weeks"][weeks[-2]] if len(weeks) > 1 else None
    return {"week": weeks[-1], "current": cur, "previous": prev, "prev_week": weeks[-2] if prev else None}


def roy_sources() -> dict:
    return RV._read(_path("roy_sources.json"), {"sources": []})


# ---------------------------------------------------------------- 레이스 보드

def game_logs(pid: int, s: str) -> list[dict]:
    def fetch():
        from nba_api.stats.endpoints import playergamelogs
        df = RV._stats_call(lambda: playergamelogs.PlayerGameLogs(player_id_nullable=pid, season_nullable=s,
                                                                   season_type_nullable="Regular Season", timeout=30).get_data_frames()[0])
        time.sleep(0.6)
        return sorted([{"date": r.GAME_DATE[:10], "game_id": r.GAME_ID, "matchup": r.MATCHUP, "pts": int(r.PTS), "reb": int(r.REB),
                        "ast": int(r.AST), "fga": int(r.FGA), "fta": int(r.FTA)} for r in df.itertuples()], key=lambda g: g["date"])
    finished = s < RV._season_of(datetime.now(timezone.utc))
    return _cached(f"logs_{pid}_{s}.json", fetch, None if finished else timedelta(hours=3))


def _pct_rank(vals: dict[int, float | None]) -> dict[int, float]:
    """후보 안에서 0(최하)~1(최고). 값이 없으면 0.5."""
    known = {k: v for k, v in vals.items() if v is not None}
    out = {k: 0.5 for k in vals}
    if len(known) < 2:
        return out
    order = sorted(known, key=lambda k: known[k])
    for i, k in enumerate(order):
        out[k] = i / (len(order) - 1)
    return out


WEIGHTS = {"pts": 1, "reb": 1, "ast": 1, "ts": 1, "team": 1, "odds": 2}


def board(s: str) -> dict:
    odds = roy_odds(s)
    rookies = rookie_stats(s)
    by_name = {_norm(p["name"]): p for p in all_players(s)}
    st = standings(s)
    team_rec = {r["team"]: r for c in ("East", "West") for r in st[c]} if "정규시즌" in st["label"] else {}
    prev_odds = {r["name"]: r for r in (odds["previous"] or {}).get("rows", [])}
    cands = []
    for r in odds["current"]["rows"]:
        p = by_name.get(_norm(r["name"]))
        pid = p["id"] if p else None
        stat = rookies.get(pid) if pid else None
        rec = team_rec.get(r["team"])
        po = prev_odds.get(r["name"])
        cands.append({**r, "id": pid, "name_ko": ko_name(r["name"]), "stats": stat,
                      "team_record": f"{rec['w']}-{rec['l']}" if rec else None, "team_pct": rec["pct"] if rec and rec["w"] + rec["l"] else None,
                      "odds_prev": po["odds"] if po else None, "implied_prev": po["implied"] if po else None})
    keys = {"pts": lambda c: (c["stats"] or {}).get("pts"), "reb": lambda c: (c["stats"] or {}).get("reb"),
            "ast": lambda c: (c["stats"] or {}).get("ast"), "ts": lambda c: (c["stats"] or {}).get("ts"),
            "team": lambda c: c["team_pct"], "odds": lambda c: c["implied"]}
    ranks = {k: _pct_rank({i: f(c) for i, c in enumerate(cands)}) for k, f in keys.items()}
    for i, c in enumerate(cands):
        c["score"] = round(sum(WEIGHTS[k] * ranks[k][i] for k in keys) / sum(WEIGHTS.values()), 3)
    cands.sort(key=lambda c: (-c["score"], -c["implied"]))
    for n, c in enumerate(cands, 1):
        c["rank"] = n
    # 지난주 대비 ▲▼: 주별 마지막 순위를 저장
    wpath = _path(f"roy_weekly_{s}.json")
    weekly = RV._read(wpath, {})
    wk = _iso_week()
    weekly[wk] = {c["name"]: c["rank"] for c in cands}
    RV._write(wpath, weekly)
    prev_wk = max((w for w in weekly if w < wk), default=None)
    for c in cands:
        pr = (weekly.get(prev_wk) or {}).get(c["name"]) if prev_wk else None
        c["rank_prev"] = pr
    # 성장 곡선: 상위 4명 경기별 득점 + 5경기 이동평균
    curves = []
    for c in cands[:4]:
        if not c["id"]:
            continue
        logs = game_logs(c["id"], s)
        pts = [g["pts"] for g in logs]
        ma = [round(sum(pts[max(0, i - 4):i + 1]) / len(pts[max(0, i - 4):i + 1]), 1) for i in range(len(pts))]
        curves.append({"name": c["name"], "name_ko": c["name_ko"], "team": c["team"], "pts": pts, "ma5": ma,
                       "dates": [g["date"] for g in logs]})
    # 역대 비교: 최근 신인왕 3명의 경기별 기록 (같은 경기 수 시점 평균은 화면에서 계산)
    past = []
    for ps, pid, name, team in PAST_ROY:
        logs = game_logs(pid, ps)
        past.append({"season": ps, "id": pid, "name": name, "name_ko": ko_name(name), "team": team,
                     "games": [{k: g[k] for k in ("pts", "reb", "ast", "fga", "fta")} for g in logs]})
    for c in cands:
        if c["id"]:
            c["games"] = [{k: g[k] for k in ("pts", "reb", "ast", "fga", "fta")} for g in game_logs(c["id"], s)] if c in cands[:6] else []
    _track_pick(s, cands)
    return {"season": s, "odds": {k: odds[k] for k in ("week", "prev_week")}, "provider": odds["current"]["provider"],
            "odds_fetched_utc": odds["current"]["fetched_utc"], "odds_url": odds["current"]["url"],
            "cands": cands, "curves": curves, "past": past, "sources": roy_sources(), "rank_prev_week": prev_wk,
            "weights": WEIGHTS, "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stats_source": {"name": "NBA.com 신인 기록 (stats.nba.com · 신인 필터)", "url": "https://www.nba.com/stats/players/traditional?PlayerExperience=Rookie"}}


def _norm(name: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


# ---------------------------------------------------------------- 나의 신인왕 픽

def picks() -> dict:
    return RV._read(PICKS_PATH, {"seasons": {}})


def set_pick(s: str, name: str, team: str, pid: int | None) -> dict:
    with RV._lock:
        data = picks()
        cur = data["seasons"].setdefault(s, {"history": [], "changes": []})
        if cur.get("pick") and cur["pick"]["name"] != name:
            cur["changes"].append({**cur["pick"], "until_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            cur["history"] = []
        cur["pick"] = {"name": name, "team": team, "id": pid, "picked_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        RV._write(PICKS_PATH, data)
        return cur


def _track_pick(s: str, cands: list[dict]) -> None:
    """하루 한 번 내 픽의 레이스 순위를 기록 (시즌 내내 추적)."""
    with RV._lock:
        data = picks()
        cur = data["seasons"].get(s)
        if not cur or not cur.get("pick"):
            return
        c = next((c for c in cands if c["name"] == cur["pick"]["name"]), None)
        today = datetime.now(T.KST).date().isoformat()
        entry = {"date": today, "rank": c["rank"] if c else None, "odds": c["odds"] if c else None}
        if cur["history"] and cur["history"][-1]["date"] == today:
            cur["history"][-1] = entry
        else:
            cur["history"].append(entry)
        RV._write(PICKS_PATH, data)


# ---------------------------------------------------------------- 리그 소식 (매일 07:00)

RUMOR = re.compile(r"\b(rumou?rs?|could|might|would|eye(s|ing)?|interest(ed)?|linked|targets?|pursu\w+|monitor\w*|explor\w+|"
                   r"in talks|discuss\w*|consider\w*|expected to|mock|predict\w*|ideas?|hypothetical|pitch|what if|should)\b|\?", re.I)
# 여러 소식을 모은 정리·소문 모음 기사 (한 줄 소식이 아니다)
ROUNDUP = re.compile(r"\b(buzz|intel|latest on|roundup|rankings?|takeaways|guide|grades|every team|power rank\w*|notebook)\b", re.I)
CATS = [
    ("trade", re.compile(r"\b(trade[sd]?|acquir\w+|deal sends|swap|traded)\b", re.I), 9),
    ("buzzer", re.compile(r"\b(buzzer|game-winn\w+|walk-off|at the horn|winner at)\b", re.I), 8),
    ("injury", re.compile(r"\b(injur\w+|torn|tear|surgery|sprain\w*|fractur\w+|out indefinitely|ruled out|miss(es)? )", re.I), 7),
    ("feat", re.compile(r"\b(\d{2}-point|triple-double|career-high|record)\b", re.I), 6),
]
CAT_KO = {"trade": "트레이드", "buzzer": "버저비터", "injury": "스타 부상", "feat": "대기록", "streak": "연승·연패", "roy": "신인왕 후보"}


def ex_knicks(s: str) -> set[int]:
    def fetch():
        from nba_api.stats.endpoints import commonteamroster
        ids = set()
        y = int(s[:4])
        for k in range(1, 6):
            ss = f"{y - k}-{(y - k + 1) % 100:02d}"
            df = RV._stats_call(lambda: commonteamroster.CommonTeamRoster(team_id=1610612752, season=ss, timeout=30).get_data_frames()[0])
            ids |= {int(x) for x in df.PLAYER_ID}
            time.sleep(0.6)
        return sorted(ids)
    cur = {p["id"] for p in RV._roster("NYK", s)}
    return set(_cached(f"exknicks_{s}.json", fetch, None)) - cur


def stars(s: str) -> dict[str, dict]:
    """지난 시즌 경기당 득점 상위 60명 = '스타' (부상 소식 선정 기준)."""
    prev = RV._prev(s)

    def fetch():
        from nba_api.stats.endpoints import leaguedashplayerstats
        df = RV._stats_call(lambda: leaguedashplayerstats.LeagueDashPlayerStats(season=prev, per_mode_detailed="PerGame", timeout=30).get_data_frames()[0])
        df = df[df.GP >= 40].sort_values("PTS", ascending=False).head(60)
        return {r.PLAYER_NAME: {"id": int(r.PLAYER_ID), "pts": round(r.PTS, 1)} for r in df.itertuples()}
    return _cached(f"stars_{prev}.json", fetch, None)


def _feats(s: str, day_et: str) -> list[dict]:
    """전날(미국 날짜) 정규시즌·플레이오프 경기의 40점 이상·트리플더블 (공식 기록)."""
    from nba_api.stats.endpoints import playergamelogs
    out = []
    for st in ("Regular Season", "Playoffs"):
        df = RV._stats_call(lambda: playergamelogs.PlayerGameLogs(season_nullable=s, season_type_nullable=st, date_from_nullable=day_et,
                                                                   date_to_nullable=day_et, timeout=30).get_data_frames()[0])
        for r in df.itertuples():
            tens = sum(v >= 10 for v in (r.PTS, r.REB, r.AST, r.STL, r.BLK))
            if r.PTS >= 40 or tens >= 3:
                out.append({"id": int(r.PLAYER_ID), "name": r.PLAYER_NAME, "team": r.TEAM_ABBREVIATION, "matchup": r.MATCHUP,
                            "pts": int(r.PTS), "reb": int(r.REB), "ast": int(r.AST), "td": tens >= 3, "game_id": r.GAME_ID, "wl": r.WL})
    return out


def build_news(s: str, next_opp: str | None, now: datetime | None = None, on_recover=None) -> dict:
    now = now or datetime.now(T.KST)   # now 는 지난 날짜로 시험할 때만 넣는다
    # 07:00 KST = 미국 동부 전날 18:00 → 끝난 '어젯밤' 경기는 미국 날짜로 그 하루 전
    day_et = ((now - timedelta(hours=13)).date() - timedelta(days=1)).strftime("%m/%d/%Y")
    g = _glossary()
    items, errors = [], []
    exk = ex_knicks(s)
    opp_ids = {p["id"] for p in RV._roster(next_opp, s)} if next_opp else set()
    roy = {}
    try:
        roy = {r["name"]: r for r in roy_odds(s)["current"]["rows"]}
    except Exception as exc:
        errors.append(RS.friendly("신인왕 배당", exc))
    players = {p["name"]: p for p in all_players(s)}

    def bonus(names: list[str], teams: list[str]) -> tuple[int, list[str]]:
        b, why = 0, []
        ids = {players[n]["id"] for n in names if n in players}
        if next_opp and (next_opp in teams or ids & opp_ids):
            b += 6; why.append(f"닉스 다음 상대 {next_opp}")
        if ids & exk:
            b += 5; why.append("전 닉스 선수")
        if any(t in WEST for t in teams):
            b += 2
        return b, why

    # 1) 공식 기록: 40점 이상·트리플더블
    try:
        for f in _feats(s, day_et):
            nm = ko_name(f["name"])
            what = "트리플더블" if f["td"] else f"{f['pts']}점"
            opp = f["matchup"].split()[-1]
            text = f"{nm}({f['team']}){briefing._josa(nm, '가') if nm[-1] >= '가' else '가'} {opp}전에서 {f['pts']}점 {f['reb']}리바운드 {f['ast']}어시스트{' 트리플더블' if f['td'] else ''} ({'승' if f['wl'] == 'W' else '패'})"
            b, why = bonus([f["name"]], [f["team"], opp])
            items.append({"cat": "feat", "score": 7 + max(0, f["pts"] - 40) // 3 + (2 if f["td"] and f["pts"] >= 30 else 0) + b,
                          "text": text, "why": why, "links": [{"name": "NBA.com 박스스코어", "url": f"https://www.nba.com/game/{f['game_id']}/box-score"}],
                          "key": f"feat:{f['id']}", "teams": [f["team"]], "basis": "공식 기록"})
    except Exception as exc:
        errors.append(RS.friendly("어젯밤 경기 기록", exc))
    # 2) 공식 순위: 5연승·5연패 이상
    try:
        st = standings(s)
        if "정규시즌" in st["label"]:
            for c in ("East", "West"):
                for r in st[c]:
                    n = abs(r["streak_n"])
                    if n >= 5:
                        win = r["streak"].startswith("W")
                        b, why = bonus([], [r["team"]])
                        items.append({"cat": "streak", "score": 4 + n // 2 + b, "key": f"streak:{r['team']}",
                                      "text": f"{TEAM_KO.get(r['team'], r['team'])} {n}{'연승' if win else '연패'} 중 ({r['w']}승 {r['l']}패, {c == 'East' and '동부' or '서부'} {r['rank']}위)",
                                      "why": why, "links": [st["source"]], "teams": [r["team"]], "basis": "공식 순위"})
    except Exception as exc:
        errors.append(RS.friendly("순위", exc))
    # 3) ESPN 기사: 트레이드·버저비터·스타 부상·대기록·신인왕 후보 (루머 제외)
    try:
        star = stars(s)
        arts = RS.fetch("news_league", lambda: briefing._get(f"{briefing.ESPN}/news", limit=50).get("articles", []),
                        _path("news_espn_last.json"), on_recover=on_recover)   # 실패하면 마지막 기사 목록 + 자동 재시도
        since = now.astimezone(timezone.utc) - timedelta(hours=30)
        for a in arts:
            head, desc = a.get("headline", ""), a.get("description", "")
            if a.get("type") not in briefing.NEWS_TYPES or datetime.fromisoformat(a["published"].replace("Z", "+00:00")) < since:
                continue
            text = f"{head} {desc}"
            if RUMOR.search(head) or ROUNDUP.search(head) or any(k in head.lower() for k in briefing.NEWS_EXCLUDE):
                continue
            names = [n for n in list(star) + list(roy) + list(players) if len(n) > 5 and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text)]
            names = list(dict.fromkeys(names))[:4]
            teams = [ESPN_TO_TRI.get(c.get("teamId")) for c in a.get("categories", []) if c.get("type") == "team"]
            teams = [t for t in teams if t]
            cat, base = None, 0
            for k, rx, w in CATS:
                if rx.search(head):
                    cat, base = k, w
                    break
            if cat == "injury" and not any(n in star for n in names):
                cat = None            # 스타가 아닌 선수의 부상은 빼기
            if not cat and any(n in roy for n in names):
                cat, base = "roy", 5
            if not cat or len(teams) >= briefing.NEWS_ROUNDUP_TEAMS:
                continue
            b, why = bonus(names, teams)
            items.append({"cat": cat, "score": base + b, "head": head, "article_id": a["id"], "key": f"art:{a['id']}",
                          "links": [{"name": "ESPN 원문", "url": a.get("links", {}).get("web", {}).get("href")}],
                          "why": why, "teams": teams, "names": names, "basis": "ESPN 기사", "published": a["published"]})
    except Exception as exc:
        errors.append(RS.friendly("ESPN 뉴스", exc))
    # 선정: 점수순, 같은 종류는 최대 2개, 같은 선수 중복 없이
    items.sort(key=lambda x: -x["score"])
    picked, per_cat, seen_names = [], {}, set()
    for it in items:
        if per_cat.get(it["cat"], 0) >= 2:
            continue
        nm = set(it.get("names", [])) | ({it["key"].split(":")[1]} if it["cat"] == "feat" else set())
        if nm & seen_names:
            continue
        picked.append(it)
        per_cat[it["cat"]] = per_cat.get(it["cat"], 0) + 1
        seen_names |= nm
        if len(picked) >= NEWS_PICK:
            break
    # 기사 제목 한 줄 한국어 요약: 표준 한글 이름 보호 번역 + 존칭·조사 다듬기
    for it in picked:
        if "head" in it:
            ko = briefing._machine_translate(it["head"])
            it["text"] = briefing.polish_ko(ko) if ko else None
            it["text_en"] = it["head"]
    picked.sort(key=lambda x: (not x["why"], -x["score"]))   # 닉스 관련 우선 표시
    return {"date_kst": now.date().isoformat(), "built_kst": now.isoformat(timespec="seconds"), "next_opp": next_opp,
            "items": picked, "candidates": len(items), "errors": errors, "et_day": day_et,
            "rule": "재미 점수: 트레이드 9 · 버저비터 8 · 스타 부상 7 · 40점/트리플더블 7+ · 연승·연패 4+ · 신인왕 후보 5, "
                    "닉스 다음 상대 +6 · 전 닉스 선수 +5 · 서부 팀 +2. 같은 종류 최대 2개. 루머·의견성 기사 제외."}


class LeagueDesk:
    BOARD_STATE = os.path.join(DIR, "board_state.json")   # 신인왕 보드: 하루 한 번 (desk_day), 서버를 다시 켜도 이어 쓴다

    def __init__(self, next_opp_fn):
        self.next_opp_fn = next_opp_fn
        self.board: dict | None = None
        self.board_at = 0.0
        self.board_day: str | None = None
        saved = RV._read(self.BOARD_STATE, None)
        if saved:
            self.board, self.board_at, self.board_day = saved["board"], saved["at"], saved["day"]
        self.board_err: str | None = None
        self.news: dict | None = None
        self.news_err: str | None = None
        self.busy: set[str] = set()

    def _bg(self, name: str, fn) -> None:
        if name in self.busy:
            return
        self.busy.add(name)

        def run():
            try:
                fn()
            except Exception as exc:
                setattr(self, f"{name}_err", RS.friendly({"board": "신인왕 기록", "news": "리그 소식"}.get(name, name), exc,
                                                         then="잠시 뒤 다시 시도"))
                print(f"[리그] {name} 실패 ({type(exc).__name__}) → logs/fetch_errors.log", flush=True)
            finally:
                self.busy.discard(name)
        threading.Thread(target=run, daemon=True).start()

    def _build_board(self):
        self.board, self.board_err, self.board_at = board(season()), None, time.time()
        self.board_day = NH.desk_day()
        RV._write(self.BOARD_STATE, {"board": self.board, "at": self.board_at, "day": self.board_day})
        print(f"[리그] 신인왕 보드: {', '.join(c['name'] for c in self.board['cands'])}", flush=True)

    def _build_news(self):
        d = build_news(season(), self.next_opp_fn(), on_recover=lambda: self._bg("news", self._build_news))
        RV._write(_path(f"news_{season()}_{d['date_kst']}.json"), d)
        self.news, self.news_err = d, None
        print(f"[리그] 오늘의 리그 소식 {len(d['items'])}개 (후보 {d['candidates']}개)", flush=True)

    def tick(self) -> None:
        # 하루 한 번, 닉스 경기 시간이 아닐 때만 (처음 설치해 보드가 아예 없을 때는 바로)
        if not self.board or (self.board_day != NH.desk_day() and not NH.knicks_busy()):
            self._bg("board", self._build_board)
        now = datetime.now(T.KST)
        today = now.date().isoformat()
        if not self.news:
            files = sorted(f for f in os.listdir(DIR) if f.startswith(f"news_{season()}_")) if os.path.isdir(DIR) else []
            if files:
                self.news = RV._read(os.path.join(DIR, files[-1]), None)
        due = now.hour >= NEWS_HOUR_KST
        if due and (not self.news or self.news.get("date_kst") != today):
            self._bg("news", self._build_news)
        elif not self.news:   # 처음 설치: 07:00 전이라도 한 번은 만든다
            self._bg("news", self._build_news)

    def snapshot(self, s_override: str | None = None) -> dict:
        st = None
        try:
            st = standings(season())
        except Exception as exc:
            st = {"error": RS.friendly("순위", exc)}
        return {"kind": "league", "season": season(), "board": self.board, "board_error": self.board_err,
                "board_loading": "board" in self.busy, "news": self.news, "news_error": self.news_err,
                "news_loading": "news" in self.busy, "standings": st, "picks": picks()["seasons"].get(season()),
                "next_news_kst": f"매일 {NEWS_HOUR_KST:02d}:00"}

    def roy_in_game(self, home: str, away: str) -> list[dict]:
        if not self.board:
            return []
        return [{k: c.get(k) for k in ("name", "name_ko", "team", "id", "rank", "odds", "stats")}
                for c in self.board["cands"] if c["team"] in (home, away)]
