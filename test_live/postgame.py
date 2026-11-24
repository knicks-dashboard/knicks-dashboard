"""경기 후 분석: Four factors · 결정적 런 · 실책(turnovers.py) · 클러치 (지난 경기 화면, 펼쳤을 때만 계산해서 보낸다).

데이터
- 경기 기록: NBA.com 공식 박스스코어·플레이바이플레이 (cdn.nba.com 라이브 데이터, replay_data/<게임ID>/ 에 캐시)
- 기준값: stats.nba.com 리그 팀 합계 (LeagueDashTeamStats, Totals) 로 같은 공식을 적용한 리그 전체 값
  닉스가 이번 시즌 치른 경기가 10경기 미만이면 지난 시즌(2025-26) 정규시즌 값을 쓴다.
공식
- eFG% = (FGM + 0.5×3PM) / FGA · 턴오버율 = TOV / (FGA + 0.44×FTA + TOV)
- 공격 리바운드율 = ORB / (ORB + 상대 DRB) · 자유투 비율 = FTM / FGA
- 승패에 크게 작용한 요인: Dean Oliver 가중치(eFG 40 · 턴오버 25 · 리바운드 20 · 자유투 15) × 두 팀 차이(%p)
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import league as LG
import nba_http as NH
import resilient as RS
import rivals as RV
import tracker as T
import turnovers as TV

WEIGHTS = {"efg": 0.40, "tov": 0.25, "orb": 0.20, "ftr": 0.15}
LABELS = {"efg": "eFG%", "tov": "턴오버율", "orb": "공격 리바운드율", "ftr": "자유투 비율"}
BASE_MIN_GAMES = 10
RUN_MIN = 8
RUN_OPP_MAX = 4     # 런 도중 상대 득점이 이보다 많으면 런이 끊긴 것
CLUTCH_SEC, CLUTCH_MARGIN = 300, 5


def _gamefile(gid: str, name: str, get, label: str, need_final: bool = False):
    """끝난 경기 데이터: 캐시가 없거나 깨졌거나(빈 파일) 경기 중에 받은 것이면 다시 받는다."""
    d = os.path.join(T.REPLAY_DATA, gid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    data = RS._load(path)
    if data is not None and (not need_final or data.get("gameStatus") == 3):
        return data
    return RS.fetch(f"pg_{gid}_{name}", get, path, label=label)


def _espn(gid: str, name: str) -> dict:
    """NBA 가 막혔을 때: ESPN 경기 요약으로 만든 같은 형식 파일 (리플레이와 같은 파일을 같이 쓴다)."""
    import espn_source
    key = {"live_boxscore.json": "box", "live_playbyplay.json": "pbp"}[name]
    return T._cached_json(os.path.join(T.REPLAY_DATA, gid, f"espn_{name[5:]}"), lambda: espn_source.final_game(gid)[key])


def box(gid: str) -> dict:
    from nba_api.live.nba.endpoints import boxscore
    try:
        b = _gamefile(gid, "live_boxscore.json", lambda: boxscore.BoxScore(gid, headers=T.BROWSER_HEADERS, timeout=30).get_dict()["game"],
                      "박스스코어", need_final=True)
    except Exception:
        if os.path.exists(os.path.join(T.REPLAY_DATA, gid, "live_boxscore.json")):
            raise
        b = _espn(gid, "live_boxscore.json")
    if b.get("gameStatus") != 3:
        raise RS.Unavailable("경기가 아직 끝나지 않아 분석할 수 없습니다")
    return b


def actions(gid: str) -> list[dict]:
    from nba_api.live.nba.endpoints import playbyplay
    if os.path.exists(os.path.join(T.REPLAY_DATA, gid, "espn_boxscore.json")) and \
            not os.path.exists(os.path.join(T.REPLAY_DATA, gid, "live_boxscore.json")):
        return _espn(gid, "live_playbyplay.json")["actions"]   # 박스스코어를 ESPN 으로 받았으면 플레이도 같은 출처
    p = _gamefile(gid, "live_playbyplay.json", lambda: playbyplay.PlayByPlay(gid, headers=T.BROWSER_HEADERS, timeout=30).get_dict()["game"],
                  "플레이바이플레이")
    acts = p.get("actions") or []
    if not any(a.get("actionType") == "game" for a in acts):   # 경기 중에 받아 둔 문자중계면 다시
        os.remove(os.path.join(T.REPLAY_DATA, gid, "live_playbyplay.json"))
        p = _gamefile(gid, "live_playbyplay.json", lambda: playbyplay.PlayByPlay(gid, headers=T.BROWSER_HEADERS, timeout=30).get_dict()["game"],
                      "플레이바이플레이")
        acts = p.get("actions") or []
    return acts


# ---------------------------------------------------------------- a) Four factors

def factors(t: dict, o: dict) -> dict:
    """t, o: FGM FGA FG3M FTM FTA TOV OREB DREB (팀, 상대)."""
    poss = t["FGA"] + 0.44 * t["FTA"] + t["TOV"]
    return {"efg": (t["FGM"] + 0.5 * t["FG3M"]) / t["FGA"] if t["FGA"] else None,
            "tov": t["TOV"] / poss if poss else None,
            "orb": t["OREB"] / (t["OREB"] + o["DREB"]) if t["OREB"] + o["DREB"] else None,
            "ftr": t["FTM"] / t["FGA"] if t["FGA"] else None}


def _box_totals(team: dict) -> dict:
    s = team["statistics"]
    # 리바운드는 선수 기록만 (stats.nba.com 팀 합계와 같은 기준), 턴오버는 팀 턴오버 포함 (역시 같은 기준)
    return {"FGM": s["fieldGoalsMade"], "FGA": s["fieldGoalsAttempted"], "FG3M": s["threePointersMade"],
            "FTM": s["freeThrowsMade"], "FTA": s["freeThrowsAttempted"], "TOV": s.get("turnoversTotal", s["turnovers"]),
            "OREB": s["reboundsOffensive"], "DREB": s["reboundsDefensive"]}


def _league_cached(name: str, fetch, max_age):
    """리그 기준값 캐시. 데모 모드에서는 make_demo.py 가 test_live/demo/ 에 복사해 둔 사본만 읽는다
    (league/ 는 git 에 없어서 배포한 서버에는 없다)."""
    if NH.DEMO:
        data = RS._load(os.path.join(NH.DEMO_DIR, name))
        if data is None:
            raise RS.Unavailable("리그 기준값: 데모 데이터에 없음 (make_demo.py 를 다시 실행)")
        return data
    return LG._cached(name, fetch, max_age)


def league_totals(season: str) -> dict:
    """리그 30팀 정규시즌 합계 → 같은 공식의 리그 값. 끝난 시즌은 영구 캐시."""
    def fetch():
        from nba_api.stats.endpoints import leaguedashteamstats
        df = RV._stats_call(lambda: leaguedashteamstats.LeagueDashTeamStats(
            season=season, per_mode_detailed="Totals", season_type_all_star="Regular Season", timeout=30).get_data_frames()[0])
        tot = {k: float(df[k].sum()) for k in ("FGM", "FGA", "FG3M", "FTM", "FTA", "TOV", "OREB", "DREB")}
        return {**tot, "teams": len(df), "games": int(df["GP"].sum() / 2)}
    finished = datetime.now(timezone.utc) > datetime(int(season[:4]) + 1, 7, 1, tzinfo=timezone.utc)
    return _league_cached(f"league_totals_{season}.json", fetch, None if finished else timedelta(hours=6))


def league_misc(season: str) -> dict:
    """리그 30팀 정규시즌 실책 기반 득점·속공 득점 합계 (LeagueDashTeamStats, Misc)."""
    def fetch():
        from nba_api.stats.endpoints import leaguedashteamstats
        df = RV._stats_call(lambda: leaguedashteamstats.LeagueDashTeamStats(
            season=season, measure_type_detailed_defense="Misc", per_mode_detailed="Totals",
            season_type_all_star="Regular Season", timeout=30).get_data_frames()[0])
        return {"PTS_OFF_TOV": float(df["PTS_OFF_TOV"].sum()), "PTS_FB": float(df["PTS_FB"].sum()),
                "team_games": int(df["GP"].sum()), "teams": len(df)}
    finished = datetime.now(timezone.utc) > datetime(int(season[:4]) + 1, 7, 1, tzinfo=timezone.utc)
    return _league_cached(f"league_misc_{season}.json", fetch, None if finished else timedelta(hours=6))


def tov_baseline(base: dict) -> dict:
    """Four factors 와 같은 시즌으로 경기당 한 팀 실책·실책 기반 득점·속공 득점."""
    use = base["season"]
    tot, misc = league_totals(use), league_misc(use)
    g = misc["team_games"]
    return {"season": use, "why": base["why"], "tov_pg": round(tot["TOV"] / g, 1), "pot_pg": round(misc["PTS_OFF_TOV"] / g, 1),
            "fb_pg": round(misc["PTS_FB"] / g, 1), "pts_per_tov": round(misc["PTS_OFF_TOV"] / tot["TOV"], 2),
            "source": {"name": f"stats.nba.com 리그 팀 기록 ({use} 정규시즌 · Traditional·Misc 합계)",
                       "url": f"https://www.nba.com/stats/teams/misc?Season={use}&SeasonType=Regular%20Season&PerMode=Totals"}}


def baseline(season: str, team_games: int) -> dict:
    use = season if team_games >= BASE_MIN_GAMES else RV._prev(season)
    tot = league_totals(use)
    f = factors(tot, tot)   # 리그 전체에서는 상대 DRB 합 = 리그 DRB 합
    why = (f"닉스 {season} 경기 {team_games}경기 (10경기 미만) → 지난 시즌 {use} 정규시즌 리그 전체 값"
           if use != season else f"{season} 정규시즌 리그 전체 값 (닉스 {team_games}경기)")
    return {"season": use, "values": f, "why": why, "games": tot["games"],
            "source": {"name": f"stats.nba.com 리그 팀 합계 ({use} 정규시즌)",
                       "url": f"https://www.nba.com/stats/teams/traditional?Season={use}&SeasonType=Regular%20Season&PerMode=Totals"}}


def four_factors(b: dict, focus: str, base: dict) -> dict:
    sides = {b[s]["teamTricode"]: b[s] for s in ("homeTeam", "awayTeam")}
    opp = next(t for t in sides if t != focus)
    tt = {t: _box_totals(sides[t]) for t in sides}
    f = {focus: factors(tt[focus], tt[opp]), opp: factors(tt[opp], tt[focus])}
    won = sides[focus]["score"] > sides[opp]["score"]
    rows = []
    for k in WEIGHTS:
        mine, theirs, bv = f[focus][k], f[opp][k], base["values"][k]
        edge = (theirs - mine) if k == "tov" else (mine - theirs)    # 닉스에 유리하면 +
        rows.append({"key": k, "label": LABELS[k], "weight": WEIGHTS[k], "lower_better": k == "tov",
                     "values": {focus: round(mine, 4), opp: round(theirs, 4)}, "base": round(bv, 4),
                     "vs_base": {focus: round(mine - bv, 4), opp: round(theirs - bv, 4)},
                     "edge_pp": round(edge * 100, 1), "impact": round(WEIGHTS[k] * edge * 100, 2)})
    # 승패와 같은 방향으로 가장 크게 작용한 요인 1~2개 (두 번째는 첫 번째의 절반 이상일 때만)
    sign = 1 if won else -1
    cands = sorted([r for r in rows if r["impact"] * sign > 0], key=lambda r: -abs(r["impact"]))
    keys = [r["key"] for r in cands[:1]] + [r["key"] for r in cands[1:2] if abs(r["impact"]) >= abs(cands[0]["impact"]) / 2]
    for r in rows:
        r["key_factor"] = r["key"] in keys
    return {"rows": rows, "opp": opp, "won": won, "key_factors": keys, "totals": tt}


# ---------------------------------------------------------------- b) 결정적 런

def _label(a: dict) -> str:
    c = T.clock_sec(a["clock"])
    q = f"{a['period']}Q" if a["period"] <= 4 else ("OT" if a["period"] == 5 else f"{a['period'] - 4}OT")
    return f"{q} {int(c // 60)}:{int(c % 60):02d}"


def scoring_events(acts: list[dict]) -> list[dict]:
    ev, h0, a0 = [], 0, 0
    for idx, a in enumerate(acts):
        h, w = T.to_int(a.get("scoreHome")), T.to_int(a.get("scoreAway"))
        if h is None or w is None or (h, w) == (h0, a0):
            continue
        ev.append({"a": a, "idx": idx, "before": (h0, a0), "after": (h, w), "dh": h - h0, "da": w - a0})
        h0, a0 = h, w
    return ev


def best_run(ev: list[dict], sign: int) -> tuple[int, int, int] | None:
    """이 팀이 상대보다 가장 많이 더 넣은 '런' (10-0, 12-2 같은 몰아치기).
    이 팀 득점으로 시작해 이 팀 득점으로 끝나고, 그 사이 상대 득점이 RUN_OPP_MAX 점 이하인 구간 중 득점 차가 가장 큰 것.
    같은 득점 차면 더 짧은 구간."""
    best = None
    for i, e in enumerate(ev):
        if sign * (e["dh"] - e["da"]) <= 0:
            continue
        mine = theirs = 0
        for j in range(i, len(ev)):
            m, t = (ev[j]["dh"], ev[j]["da"]) if sign > 0 else (ev[j]["da"], ev[j]["dh"])
            theirs += t
            if theirs > RUN_OPP_MAX:
                break
            mine += m
            if m and (best is None or mine - theirs > best[2] or (mine - theirs == best[2] and j - i < best[1] - best[0])):
                best = (i, j, mine - theirs)
    return best


def runs(ev: list[dict], home: str, away: str, focus: str) -> dict:
    out = {}
    for tri, sign in ((home, 1), (away, -1)):
        r = best_run(ev, sign)
        if not r or r[2] < RUN_MIN:
            out[tri] = None
            continue
        s, e, _ = r
        seg = ev[s:e + 1]
        mine = sum(x["dh"] if sign > 0 else x["da"] for x in seg)
        theirs = sum(x["da"] if sign > 0 else x["dh"] for x in seg)
        who: dict[str, int] = {}
        for x in seg:
            pts = x["dh"] if sign > 0 else x["da"]
            if pts and x["a"].get("teamTricode") == tri and x["a"].get("playerNameI"):
                who[x["a"]["playerNameI"]] = who.get(x["a"]["playerNameI"], 0) + pts
        b, a = seg[0]["before"], seg[-1]["after"]
        out[tri] = {"team": tri, "pts": mine, "opp_pts": theirs, "text": f"{mine}-{theirs}",
                    "start": _label(seg[0]["a"]), "end": _label(seg[-1]["a"]),
                    "score_from": f"{away} {b[1]} - {b[0]} {home}", "score_to": f"{away} {a[1]} - {a[0]} {home}",
                    "leaders": [{"name": n, "pts": p} for n, p in sorted(who.items(), key=lambda kv: -kv[1])[:3]],
                    # 실책 섹션에서 런과 겹치는 실책을 찾는 데 쓴다 (i_prev = 런 직전 득점 플레이)
                    "i_prev": ev[s - 1]["idx"] if s else -1, "i_from": seg[0]["idx"], "i_to": seg[-1]["idx"],
                    "t_from": round(T.elapsed(seg[0]["a"]["period"], T.clock_sec(seg[0]["a"]["clock"])), 1),
                    "t_to": round(T.elapsed(seg[-1]["a"]["period"], T.clock_sec(seg[-1]["a"]["clock"])), 1)}
    opp = away if focus == home else home
    return {"focus": out[focus], "opp": out[opp], "min": RUN_MIN}


# ---------------------------------------------------------------- c) 클러치

def clutch(acts: list[dict], home: str, away: str) -> dict:
    """4쿼터·연장 남은 시간 5:00 이하, 그 플레이 직전 점수 차 5점 이내인 플레이만 (NBA 공식 클러치 정의)."""
    h, w = 0, 0
    teams = {t: {"pts": 0, "fgm": 0, "fga": 0, "fg3m": 0, "fg3a": 0, "ftm": 0, "fta": 0, "tov": 0} for t in (home, away)}
    players: dict[tuple[str, str], dict] = {}
    first = last = None
    secs = 0.0
    prev = None   # 직전 플레이 (쿼터, 남은 초) — 그 뒤 점수 차가 5점 이내였던 시간만 클러치 시간으로 더한다
    for a in acts:
        nh, nw = T.to_int(a.get("scoreHome")), T.to_int(a.get("scoreAway"))
        per, clk = a.get("period", 0), T.clock_sec(a.get("clock"))
        inside = per >= 4 and clk <= CLUTCH_SEC and abs(h - w) <= CLUTCH_MARGIN
        if prev and prev[0] == per and per >= 4 and abs(h - w) <= CLUTCH_MARGIN:
            secs += max(0.0, min(prev[1], CLUTCH_SEC) - clk)
        prev = (per, clk)
        tri = a.get("teamTricode")
        if inside and tri in teams:
            first = first or a
            last = a
            t = teams[tri]
            p = players.setdefault((tri, a.get("playerNameI") or "팀"), {"team": tri, "name": a.get("playerNameI") or "팀",
                                                                          "pts": 0, "fgm": 0, "fga": 0, "ftm": 0, "fta": 0, "tov": 0})
            kind, made = a.get("actionType"), a.get("shotResult") == "Made"
            if kind in ("2pt", "3pt"):
                for x in (t, p):
                    x["fga"] += 1
                    x["fgm"] += made
                    x["pts"] += (3 if kind == "3pt" else 2) if made else 0
                if kind == "3pt":
                    t["fg3a"] += 1
                    t["fg3m"] += made
            elif kind == "freethrow":
                for x in (t, p):
                    x["fta"] += 1
                    x["ftm"] += made
                    x["pts"] += 1 if made else 0
            elif kind == "turnover":
                for x in (t, p):
                    x["tov"] += 1
        if nh is not None and nw is not None:
            h, w = nh, nw
    if not first:
        return {"had": False, "rule": "4쿼터·연장 남은 시간 5:00 이하, 점수 차 5점 이내"}
    top = sorted((p for p in players.values() if p["pts"] or p["fga"] or p["fta"] or p["tov"]),
                 key=lambda p: (-p["pts"], -p["fga"]))
    return {"had": True, "teams": teams, "players": top[:6], "from": _label(first), "to": _label(last),
            "seconds": round(secs), "rule": "4쿼터·연장 남은 시간 5:00 이하, 점수 차 5점 이내 (그 플레이 직전 점수 기준)"}


# ---------------------------------------------------------------- 조립

def build(gid: str, focus: str, team_games: int) -> dict:
    """team_games: 이 경기까지 닉스가 이번 시즌 치른 정규시즌 경기 수 (기준값 고르기용)."""
    b = box(gid)
    acts = actions(gid)
    home, away = b["homeTeam"]["teamTricode"], b["awayTeam"]["teamTricode"]
    espn = not os.path.exists(os.path.join(T.REPLAY_DATA, gid, "live_boxscore.json"))
    season = RV._season_of(datetime.fromisoformat(b["gameTimeUTC"].replace("Z", "+00:00")))
    base = baseline(season, team_games)
    ff = four_factors(b, focus, base)
    ev = scoring_events(acts)
    rn = runs(ev, home, away, focus)
    starters = {b[x]["teamTricode"]: {p["personId"] for p in b[x]["players"] if p.get("starter") == "1"} for x in ("homeTeam", "awayTeam")}
    names = {p["personId"]: p.get("nameI") or p.get("name") for x in ("homeTeam", "awayTeam") for p in b[x]["players"]}
    tov = TV.postgame(acts, home, away, focus, starters, names, rn, tov_baseline(base))
    official = {b[x]["teamTricode"]: b[x]["statistics"].get("pointsFromTurnovers") for x in ("homeTeam", "awayTeam")}
    tov["official_pot"] = official   # 박스스코어 공식 값 (같은지 화면에서 확인용)
    return {"gid": gid, "home": home, "away": away, "focus": focus, "opp": ff["opp"],
            "score": {home: b["homeTeam"]["score"], away: b["awayTeam"]["score"]},
            "four_factors": ff, "baseline": base, "runs": rn, "turnovers": tov, "clutch": clutch(acts, home, away),
            "computed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_as_of": RS.as_of(os.path.join(T.REPLAY_DATA, gid, "live_playbyplay.json")
                                   if not espn else os.path.join(T.REPLAY_DATA, gid, "espn_playbyplay.json")),
            "data_source": "ESPN" if espn else "NBA.com",
            "sources": ([{"name": "ESPN 경기 요약 (NBA.com 접속 차단으로 대체)", "url": "https://www.espn.com/nba/scoreboard"}] if espn else
                        [{"name": "NBA.com 박스스코어", "url": f"https://www.nba.com/game/{gid}/box-score"},
                         {"name": "NBA.com 플레이바이플레이", "url": f"https://www.nba.com/game/{gid}/play-by-play"}]) + [
                        base["source"], tov["baseline"]["source"]],
            "weights_rule": "요인별 영향 = Dean Oliver 가중치(eFG 40% · 턴오버 25% · 공격 리바운드 20% · 자유투 15%) × 두 팀 차이(%p). "
                            "승패와 같은 방향으로 가장 큰 요인을 강조 (두 번째는 첫 번째의 절반 이상일 때만)."}
