"""공통 이벤트 목록 → 대시보드 스냅샷.

매번 처음부터 다시 계산한다 (한 경기 이벤트는 수백 개라 충분히 빠르다).
라이브 피드는 지난 이벤트를 수정·삭제하기도 하므로 증분 계산보다 이쪽이 안전하다.
"""
from __future__ import annotations

from collections import defaultdict

from . import insights
from . import model as m
from .lineups import Starters

STAT_KEYS = ["pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb",
             "ast", "stl", "blk", "tov", "pf", "pm"]
RUN_MIN_MARGIN = 8   # 런: 상대보다 8점 이상 많이 넣고
RUN_OPP_RATIO = 4    #      상대 득점이 내 득점의 1/4 이하 (8-0, 10-2, 12-3 ...)
RECENT_PLAYS = 14


def build_snapshot(meta: m.GameMeta, events: list[m.Event], starters: Starters,
                   now_elapsed: float, focus: str, baseline: dict | None) -> dict:
    tris = [meta.home.tricode, meta.away.tricode]
    opp = meta.other(focus)
    players = {p.id: (t.tricode, p) for t in (meta.home, meta.away) for p in t.players}

    stats: dict[int, dict] = defaultdict(lambda: dict.fromkeys(STAT_KEYS, 0))
    team_extra = {t: {"oreb": 0, "dreb": 0, "tov": 0} for t in tris}  # 팀 리바운드/팀 턴오버
    minutes: dict[int, float] = defaultdict(float)
    on_since: dict[int, float] = {}
    lineup: dict[str, list[int]] = {t: [] for t in tris}
    score = dict.fromkeys(tris, 0)
    linescore: dict[int, dict[str, int]] = {}
    scoring: list[dict] = []
    seen_periods: set[int] = set()
    # 자유투 +-는 파울 순간 코트에 있던 선수에게 준다 (자유투 사이 교체 반영 안 함)
    foul_lineup: tuple[int, float, dict[str, list[int]]] | None = None
    foul_log: list[dict] = []

    def set_lineup(tri: str, new: list[int], t: float) -> None:
        for pid in lineup[tri]:
            if pid not in new and pid in on_since:
                minutes[pid] += t - on_since.pop(pid)
        for pid in new:
            on_since.setdefault(pid, t)
        lineup[tri] = list(new)

    for e in events:
        t = e.elapsed
        if e.period not in seen_periods:
            seen_periods.add(e.period)
            linescore[e.period] = dict.fromkeys(tris, 0)
            for tri in tris:
                start = starters.get(e.period, {}).get(tri)
                if start:
                    set_lineup(tri, start, m.period_start_elapsed(e.period))
        if e.kind == m.SUB and e.team in lineup:
            new = [p for p in lineup[e.team] if p != e.out_id]
            if e.player_id and e.player_id not in new:
                new.append(e.player_id)
            set_lineup(e.team, new, t)
            continue

        s = stats[e.player_id] if e.player_id else None
        if e.kind == m.SHOT:
            if s:
                s["fga"] += 1
                s["fg3a"] += e.value == 3
            if e.made:
                if s:
                    s["fgm"] += 1
                    s["fg3m"] += e.value == 3
                if e.assist_id:
                    stats[e.assist_id]["ast"] += 1
        elif e.kind == m.FREE_THROW and s:
            s["fta"] += 1
            s["ftm"] += bool(e.made)
        elif e.kind == m.REBOUND:
            key = "oreb" if e.offensive else "dreb"
            if s:
                s[key] += 1
            elif e.team in team_extra:
                team_extra[e.team][key] += 1
        elif e.kind == m.TURNOVER:
            if s:
                s["tov"] += 1
            elif e.team in team_extra:
                team_extra[e.team]["tov"] += 1
        elif e.kind == m.STEAL and s:
            s["stl"] += 1
        elif e.kind == m.BLOCK and s:
            s["blk"] += 1
        elif e.kind == m.FOUL:
            foul_lineup = (e.period, e.clock, {k: list(v) for k, v in lineup.items()})
            if s and e.personal:
                s["pf"] += 1
                if e.player_id in players:
                    foul_log.append({"t": t, "player_id": e.player_id, "pf": s["pf"],
                                     "label": f"{m.period_label(e.period)} {m.fmt_clock(e.clock)}"})

        pts = e.value if e.kind in (m.SHOT, m.FREE_THROW) and e.made else 0
        if pts and e.team in score:
            score[e.team] += pts
            linescore[e.period][e.team] += pts
            if s:
                s["pts"] += pts
            other = meta.other(e.team)
            on = lineup
            if e.kind == m.FREE_THROW and foul_lineup and foul_lineup[:2] == (e.period, e.clock):
                on = foul_lineup[2]
            for pid in on[e.team]:
                stats[pid]["pm"] += pts
            for pid in on[other]:
                stats[pid]["pm"] -= pts
            scoring.append({"t": t, "team": e.team, "pts": pts, "home": score[meta.home.tricode],
                            "away": score[meta.away.tricode], "margin": score[focus] - score[opp],
                            "label": f"{m.period_label(e.period)} {m.fmt_clock(e.clock)}",
                            "desc": e.description})

    for pid, since in on_since.items():
        minutes[pid] += max(now_elapsed - since, 0)

    oncourt = {pid for tri in tris for pid in lineup[tri]}
    box = {tri: _team_box(meta.team(tri), stats, minutes, oncourt, team_extra[tri], baseline, focus)
           for tri in tris}
    ff = {tri: _four_factors(box[tri]["totals"], box[meta.other(tri)]["totals"]) for tri in tris}

    period, remaining = m.elapsed_to_clock(now_elapsed)
    last_period = max(seen_periods) if seen_periods else 1
    if events and events[-1].kind == m.PERIOD_END and events[-1].period == last_period:
        period, remaining = last_period, 0.0
    final = (period >= m.REGULATION_PERIODS and remaining == 0.0
             and score[meta.home.tricode] != score[meta.away.tricode])

    snap = {
        "game": {"id": meta.game_id, "title": meta.title, "focus": focus, "opponent": opp,
                 "home": _team_head(meta.home), "away": _team_head(meta.away)},
        "clock": {"period": period, "label": m.period_label(period), "remaining": m.fmt_clock(remaining),
                  "elapsed": now_elapsed},
        "score": score,
        "linescore": [{"period": p, "label": m.period_label(p), **linescore[p]} for p in sorted(linescore)],
        "margin": scoring,
        "runs": _runs(scoring),
        "current_run": _current_run(scoring),
        "box": box,
        "four_factors": ff,
        "baseline": _baseline_view(baseline, focus, opp),
        "plays": [_play(e, players) for e in events[-RECENT_PLAYS:][::-1]],
        "status_final": final,
    }
    snap["alerts"] = insights.alerts(meta, scoring, foul_log, players)
    snap["win_prob"] = insights.win_probability(meta, focus, scoring, now_elapsed)
    snap["unusual"] = insights.unusual(snap, baseline, now_elapsed)
    return snap


def _team_head(t: m.Team) -> dict:
    return {"tricode": t.tricode, "city": t.city, "name": t.name}


def _team_box(team: m.Team, stats, minutes, oncourt, extra, baseline, focus) -> dict:
    season_ppg = {}
    if baseline and team.tricode == focus:
        season_ppg = {p["PLAYER_ID"]: p["PTS"] for p in baseline["players"]["per_game"]}
    rows, totals = [], dict.fromkeys(STAT_KEYS, 0)
    for p in team.players:
        st = stats.get(p.id)
        sec = minutes.get(p.id, 0.0)
        if not st and sec == 0:
            continue
        st = st or dict.fromkeys(STAT_KEYS, 0)
        for k in STAT_KEYS:
            totals[k] += st[k]
        rows.append({"id": p.id, "name": p.name_i, "jersey": p.jersey, "starter": p.starter,
                     "oncourt": p.id in oncourt, "sec": round(sec), "min": _fmt_min(sec),
                     **st, "reb": st["oreb"] + st["dreb"], "season_ppg": season_ppg.get(p.id)})
    rows.sort(key=lambda r: (not r["starter"], -r["sec"]))
    totals["pm"] = 0
    totals["oreb"] += extra["oreb"]
    totals["dreb"] += extra["dreb"]
    totals["tov"] += extra["tov"]
    totals["reb"] = totals["oreb"] + totals["dreb"]
    return {"players": rows, "totals": totals, "team_reb": extra["oreb"] + extra["dreb"],
            "team_tov": extra["tov"]}


def _four_factors(t: dict, o: dict) -> dict:
    def div(a, b):
        return round(a / b, 3) if b else None
    return {
        "efg": div(t["fgm"] + 0.5 * t["fg3m"], t["fga"]),
        "tov": div(t["tov"], t["fga"] + 0.44 * t["fta"] + t["tov"]),
        "oreb": div(t["oreb"], t["oreb"] + o["dreb"]),
        "ftr": div(t["fta"], t["fga"]),
    }


def _baseline_view(baseline: dict | None, focus: str, opp: str) -> dict | None:
    if not baseline:
        return None
    ff = baseline["team"]["four_factors"]
    return {
        "season": baseline["meta"]["season"],
        "season_type": baseline["meta"]["season_type"],
        "four_factors": {
            focus: {"efg": ff["EFG_PCT"], "tov": ff["TM_TOV_PCT"], "oreb": ff["OREB_PCT"], "ftr": ff["FTA_RATE"]},
            # 상대팀 값은 '닉스가 시즌 평균적으로 허용한 수치'와 비교한다
            opp: {"efg": ff["OPP_EFG_PCT"], "tov": ff["OPP_TOV_PCT"], "oreb": ff["OPP_OREB_PCT"],
                  "ftr": ff["OPP_FTA_RATE"]},
        },
        "quarters": baseline["team"]["quarters"],
        "per_game": baseline["team"]["per_game"],
    }


def _runs(scoring: list[dict]) -> list[dict]:
    runs, i = [], 0
    while i < len(scoring):
        team = scoring[i]["team"]
        mine = theirs = 0
        best = None
        for j in range(i, len(scoring)):
            if scoring[j]["team"] == team:
                mine += scoring[j]["pts"]
            else:
                theirs += scoring[j]["pts"]
            if theirs * RUN_OPP_RATIO > mine + RUN_MIN_MARGIN * 2:
                break
            if (scoring[j]["team"] == team and mine - theirs >= RUN_MIN_MARGIN
                    and theirs * RUN_OPP_RATIO <= mine
                    and (best is None or mine - theirs > best[1] - best[2])):
                best = (j, mine, theirs)
        if best:
            j, mine, theirs = best
            runs.append({"team": team, "pts": mine, "opp_pts": theirs,
                         "start_t": scoring[i]["t"], "end_t": scoring[j]["t"],
                         "start_label": scoring[i]["label"], "end_label": scoring[j]["label"]})
            i = j + 1
        else:
            i += 1
    return runs


def _current_run(scoring: list[dict]) -> dict | None:
    if not scoring:
        return None
    team = scoring[-1]["team"]
    pts = 0
    for s in reversed(scoring):
        if s["team"] != team:
            break
        pts += s["pts"]
    return {"team": team, "pts": pts}


def _play(e: m.Event, players) -> dict:
    return {"label": f"{m.period_label(e.period)} {m.fmt_clock(e.clock)}", "team": e.team,
            "kind": e.kind, "desc": e.description, "scoring": bool(e.made and e.kind in (m.SHOT, m.FREE_THROW)),
            "home": e.score_home, "away": e.score_away}


def _fmt_min(sec: float) -> str:
    s = int(round(sec))
    return f"{s // 60}:{s % 60:02d}"
