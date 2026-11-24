"""실책 분석: 실책 기반 득점 · 실책 타임라인(몰림·무실책 구간) · 선수별 실책. 경기 후 분석과 라이브 화면이 같이 쓴다.

데이터: NBA.com 공식 플레이바이플레이 (cdn.nba.com 라이브 데이터). 선발은 박스스코어, 교체는 플레이바이플레이.

'실책 직후 공격권' 판정 (possession_after)
- 실책 다음 플레이부터, 공을 얻은 팀이 공격권을 잃을 때까지:
  야투 성공(같은 시각 앤드원 자유투까지) · 마지막 자유투 성공 · 그 팀의 실책 · 상대 수비 리바운드 · 쿼터 종료.
- 공격 리바운드는 같은 공격권. 플래그런트·테이크 파울·클리어 패스 자유투 뒤에는 공을 다시 가지므로 계속 이어진다.
- 이 규칙으로 계산한 실책 기반 득점은 캐시해 둔 7경기 모두 NBA 공식 박스스코어 pointsFromTurnovers 와 같다.
라이브볼 실책 = 스틸이 기록된 실책. 나머지(라인 아웃·트래블링·공격자 파울·샷클락 등)는 데드볼 실책.
"""
from __future__ import annotations

from collections import Counter

import tracker as T

BURST_N, BURST_SEC = 3, 180          # 몰림: 3분(경기 시간) 안에 실책 3개 이상
KEEP_BALL = ("flagrant", "take", "clear path", "away from play", "away-from-play")
PAUSE = ("foul", "substitution", "timeout", "stoppage", "instantreplay")
RULE = ("실책 다음 플레이부터 공을 얻은 팀이 공격권을 잃을 때까지(야투 성공·마지막 자유투 성공·실책·상대 수비 리바운드·쿼터 종료) "
        "낸 점수. 공격 리바운드는 같은 공격권으로 이어서 셉니다. 라이브볼 실책 = 스틸이 기록된 실책, "
        "속공 득점 = 라이브볼 실책 뒤 공격권에서 NBA 기록상 속공(fastbreak)으로 표시된 득점.")


def _pts(a: dict) -> int:
    if a.get("shotResult") != "Made":
        return 0
    return {"2pt": 2, "3pt": 3, "freethrow": 1}.get(a.get("actionType"), 0)


def label(a: dict) -> str:
    c = T.clock_sec(a.get("clock"))
    p = a.get("period", 1)
    q = f"{p}Q" if p <= 4 else ("OT" if p == 5 else f"{p - 4}OT")
    return f"{q} {int(c // 60)}:{int(c % 60):02d}"


def span_label(a: str, b: str) -> str:
    """'2Q 3:34', '2Q 2:39' → '2Q 3:34~2:39' (쿼터가 같으면 한 번만)."""
    qa, qb = a.split(" ")[0], b.split(" ")[0]
    return f"{a}~{b.split(' ', 1)[1]}" if qa == qb else f"{a}~{b}"


def t_of(a: dict) -> float:
    return T.elapsed(a.get("period", 1), T.clock_sec(a.get("clock")))


def possession_after(acts: list[dict], i: int, lost: str, got: str) -> dict:
    """acts[i] 가 lost 팀의 실책일 때, got 팀의 다음 공격권. end = 공격권이 끝난 다음 플레이 번호."""
    pts = fb = 0
    made_at, keep = None, False
    for j in range(i + 1, len(acts)):
        a = acts[j]
        k, tri = a.get("actionType"), a.get("teamTricode")
        if made_at:   # 야투 성공 뒤: 같은 시각의 앤드원 자유투만 이어진다
            if (a.get("period"), a.get("clock")) != made_at or not (k == "freethrow" and tri == got or k in PAUSE):
                return {"pts": pts, "fb": fb, "end": j}
        if k in ("period", "game"):
            return {"pts": pts, "fb": fb, "end": j}
        if tri == got and k == "turnover":
            return {"pts": pts, "fb": fb, "end": j}
        if k == "rebound" and a.get("subType") == "defensive" and tri == lost:
            return {"pts": pts, "fb": fb, "end": j}
        if k == "jumpball" and tri == lost and a.get("subType") == "recovered":
            return {"pts": pts, "fb": fb, "end": j}
        if k == "foul" and tri == lost:
            d = f"{a.get('description') or ''} {a.get('subType') or ''}".lower()
            keep = any(w in d for w in KEEP_BALL)
        p = _pts(a) if tri == got else 0
        pts += p
        if p and "fastbreak" in (a.get("qualifiers") or []):
            fb += p
        if k in ("2pt", "3pt") and p:
            made_at = (a.get("period"), a.get("clock"))
        if k == "freethrow":
            st = a.get("subType") or ""
            last = " of " in st and st.split(" of ")[0] == st.split(" of ")[-1]
            if last and "technical" not in st:
                if keep:            # 플래그런트·테이크 파울: 자유투 뒤 공을 다시 가진다
                    made_at, keep = None, False
                elif p:
                    return {"pts": pts, "fb": fb, "end": j + 1}
    return {"pts": pts, "fb": fb, "end": len(acts)}


def _lineups(acts: list[dict], starters: dict[str, set]) -> list[dict[str, frozenset]]:
    """각 플레이 시점에 코트 위 5명 (선발 → 교체 기록 반영)."""
    on = {t: set(s) for t, s in starters.items()}
    out = []
    for a in acts:
        tri = a.get("teamTricode")
        if a.get("actionType") == "substitution" and tri in on:
            (on[tri].add if a.get("subType") == "in" else on[tri].discard)(a.get("personId"))
        out.append({t: frozenset(s) for t, s in on.items()})
    return out


def _score(acts: list[dict], j: int) -> tuple[int, int]:
    """acts[j] 직전까지의 (홈, 원정) 점수."""
    for k in range(min(j, len(acts)) - 1, -1, -1):
        h, w = T.to_int(acts[k].get("scoreHome")), T.to_int(acts[k].get("scoreAway"))
        if h is not None and w is not None:
            return h, w
    return 0, 0


def analyze(acts: list[dict], home: str, away: str, starters: dict[str, set] | None = None,
            names: dict[int, str] | None = None) -> dict:
    """실책 이벤트·팀 합계·선수별 기록. starters 가 있으면 라인업도 계산한다."""
    names = names or {}
    lineups = _lineups(acts, starters) if starters else None
    ev = []
    for i, a in enumerate(acts):
        lost = a.get("teamTricode")
        if a.get("actionType") != "turnover" or lost not in (home, away):
            continue
        got = away if lost == home else home
        steal = a.get("stealPersonId")
        if not steal:   # 스틸이 별도 플레이로만 기록된 경우
            nxt = next((b for b in acts[i + 1:i + 3] if b.get("actionType") == "steal" and b.get("clock") == a.get("clock")), None)
            steal = nxt.get("personId") if nxt else None
        poss = possession_after(acts, i, lost, got)
        sh, sa = _score(acts, i + 1)
        team_tov = not a.get("personId") or "team" in (a.get("qualifiers") or [])
        ev.append({"i": i, "t": round(t_of(a), 1), "label": label(a), "team": lost, "pid": a.get("personId"),
                   "player": "팀 실책" if team_tov else (a.get("playerNameI") or names.get(a.get("personId")) or "?"),
                   "kind": "live" if steal else "dead",
                   "sub": a.get("subType") or "", "desc": a.get("descriptor") or "",
                   "steal_pid": steal, "steal_by": (names.get(steal) or _name_in(acts, steal)) if steal else None,
                   "opp_pts": poss["pts"], "opp_fb": poss["fb"] if steal else 0, "end": poss["end"],
                   "scored": poss["pts"] > 0, "score": f"{away} {sa} - {sh} {home}",
                   "lineup": sorted(lineups[i][lost]) if lineups else None})
    teams = {}
    for tri in (home, away):
        mine = [e for e in ev if e["team"] == tri]
        theirs = [e for e in ev if e["team"] != tri]
        teams[tri] = {"tov": len(mine), "live": sum(e["kind"] == "live" for e in mine), "dead": sum(e["kind"] == "dead" for e in mine),
                      "led_to_score": sum(e["scored"] for e in mine),
                      # 실책 기반 득점: 이 팀이 상대 실책 뒤 공격권에서 낸 점수
                      "pot": sum(e["opp_pts"] for e in theirs),
                      "pot_live": sum(e["opp_pts"] for e in theirs if e["kind"] == "live"),
                      "pot_dead": sum(e["opp_pts"] for e in theirs if e["kind"] == "dead"),
                      "fb_live": sum(e["opp_fb"] for e in theirs)}
    return {"events": ev, "teams": teams, "lineups": lineups}


def _name_in(acts: list[dict], pid) -> str | None:
    return next((a.get("playerNameI") for a in acts if a.get("personId") == pid and a.get("playerNameI")), None)


def bursts(ev: list[dict], team: str, n: int = BURST_N, sec: float = BURST_SEC) -> list[list[dict]]:
    """같은 팀 실책이 sec 초(경기 시간) 안에 n 개 이상 몰린 구간들 (겹치면 하나로 합친다)."""
    mine = [e for e in ev if e["team"] == team]
    spans: list[list[int]] = []
    for s in range(len(mine)):
        e = s
        while e + 1 < len(mine) and mine[e + 1]["t"] - mine[s]["t"] <= sec:
            e += 1
        if e - s + 1 >= n:
            if spans and s <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], e)
            else:
                spans.append([s, e])
    return [mine[s:e + 1] for s, e in spans]


# ---------------------------------------------------------------- 경기 후 분석용

def _names(ids, names: dict[int, str]) -> list[str]:
    return sorted(names.get(p, str(p)) for p in ids)


def _lineup_time(acts: list[dict], lineups, team: str, i0: int, i1: int) -> list[tuple[frozenset, float]]:
    """acts[i0:i1] 동안 그 팀 라인업별 코트 시간(초), 긴 순."""
    used: Counter = Counter()
    for k in range(max(i0, 0), min(i1, len(acts)) - 1):
        dt = t_of(acts[k + 1]) - t_of(acts[k])
        if dt > 0:
            used[lineups[k][team]] += dt
    return used.most_common()


def postgame(acts: list[dict], home: str, away: str, focus: str, starters: dict[str, set], names: dict[int, str],
             runs: dict, base: dict | None) -> dict:
    A = analyze(acts, home, away, starters, names)
    ev, lineups = A["events"], A["lineups"]
    opp = away if focus == home else home
    end_t = round(t_of(acts[-1]), 1) if acts else 2880
    periods = max((a.get("period", 1) for a in acts), default=4)

    def score_at(j):
        h, w = _score(acts, j)
        return {"home": h, "away": w, "text": f"{away} {w} - {h} {home}"}

    def delta(j0, j1, team):
        a, b = _score(acts, j0), _score(acts, j1)
        mine = (b[0] - a[0]) if team == home else (b[1] - a[1])
        theirs = (b[1] - a[1]) if team == home else (b[0] - a[0])
        return mine, theirs

    # 몰림 구간 (양 팀)
    clusters = []
    for tri in (focus, opp):
        for grp in bursts(ev, tri):
            j0, j1 = grp[0]["i"], max(e["end"] for e in grp)
            mine, theirs = delta(j0, j1, tri)
            lc = Counter(frozenset(e["lineup"]) for e in grp)
            five, cnt = lc.most_common(1)[0]
            clusters.append({"team": tri, "n": len(grp), "from": grp[0]["label"], "to": grp[-1]["label"],
                             "t0": grp[0]["t"], "t1": grp[-1]["t"], "span_sec": round(grp[-1]["t"] - grp[0]["t"]),
                             "opp_pts": sum(e["opp_pts"] for e in grp), "pts": mine, "allowed": theirs,
                             "score_from": score_at(j0)["text"], "score_to": score_at(j1)["text"],
                             "lineup": _names(five, names), "lineup_n": cnt, "lineup_same": len(lc) == 1,
                             "players": [e["player"] for e in grp]})

    # 무실책 구간 (가장 긴 것, 양 팀)
    clean = {}
    for tri in (focus, opp):
        marks = [(0.0, 0, None)] + [(e["t"], e["i"], e) for e in ev if e["team"] == tri] + [(end_t, len(acts), None)]
        best = max(zip(marks, marks[1:]), key=lambda p: p[1][0] - p[0][0])
        (ta, ia, ea), (tb, ib, eb) = best
        i0 = ia + 1 if ea else 0
        mine, theirs = delta(i0, ib, tri)
        lt = _lineup_time(acts, lineups, tri, i0, ib)
        top, sec = lt[0] if lt else (frozenset(), 0)
        clean[tri] = {"team": tri, "sec": round(tb - ta), "t0": ta, "t1": tb,
                      "from": ea["label"] if ea else "경기 시작", "to": eb["label"] if eb else "경기 끝",
                      "pts": mine, "allowed": theirs, "score_from": score_at(i0)["text"], "score_to": score_at(ib)["text"],
                      "lineup": _names(top, names), "lineup_sec": round(sec), "lineups_used": len(lt)}

    # 런과 겹치는 실책
    run_links = []
    for key, team in (("focus", focus), ("opp", opp)):
        r = runs.get(key)
        if not r or r.get("i_from") is None:
            continue
        lo, hi = r["i_prev"] + 1, r["i_to"]
        their_tov = [e for e in ev if e["team"] != team and lo <= e["i"] <= hi]
        own_tov = [e for e in ev if e["team"] == team and lo <= e["i"] <= hi]
        run_links.append({"key": key, "team": team, "text": r["text"], "from": r["start"], "to": r["end"],
                          "t0": r["t_from"], "t1": r["t_to"],
                          "opp_tov": len(their_tov), "own_tov": len(own_tov),
                          "pot_in_run": sum(e["opp_pts"] for e in their_tov if e["i"] <= hi),
                          "players": [e["player"] for e in their_tov]})

    # 선수별
    players: dict[tuple, dict] = {}
    for e in ev:
        p = players.setdefault((e["team"], e["player"]), {"team": e["team"], "name": e["player"], "tov": 0, "live": 0, "dead": 0,
                                                           "led_to_score": 0, "opp_pts": 0, "types": Counter()})
        p["tov"] += 1
        p[e["kind"]] += 1
        p["led_to_score"] += e["scored"]
        p["opp_pts"] += e["opp_pts"]
        p["types"][_kind_ko(e)] += 1
    plist = sorted(players.values(), key=lambda p: (p["team"] != focus, -p["tov"], -p["opp_pts"]))
    for p in plist:
        p["types"] = [{"type": k, "n": v} for k, v in p["types"].most_common()]
    stealers = {}
    for tri in (focus, opp):   # tri 팀 실책을 가장 많이 빼앗은 상대 선수
        c = Counter(e["steal_by"] for e in ev if e["team"] == tri and e["steal_by"])
        stealers[tri] = [{"name": n, "steals": k} for n, k in c.most_common(3)]

    # 기준값 비교 (경기당 한 팀 값)
    T_ = A["teams"]
    cmp = None
    if base:
        cmp = {"season": base["season"], "why": base["why"], "source": base["source"],
               "per_game": {"tov": base["tov_pg"], "pot": base["pot_pg"], "fb": base["fb_pg"]},
               "pts_per_tov": base["pts_per_tov"],
               "teams": {t: {"tov_vs": round(T_[t]["tov"] - base["tov_pg"], 1), "pot_vs": round(T_[t]["pot"] - base["pot_pg"], 1),
                             "pts_per_opp_tov": round(T_[t]["pot"] / T_[opp if t == focus else focus]["tov"], 2)
                             if T_[opp if t == focus else focus]["tov"] else None} for t in (focus, opp)}}

    timeline = [{"t": e["t"], "team": e["team"], "label": e["label"], "player": e["player"], "kind": e["kind"],
                 "type": _kind_ko(e), "scored": e["scored"], "opp_pts": e["opp_pts"], "steal_by": e["steal_by"], "score": e["score"]}
                for e in ev]
    return {"teams": T_, "timeline": timeline, "end_t": end_t, "periods": periods, "clusters": clusters, "clean": clean,
            "runs": run_links, "players": plist, "stealers": stealers, "baseline": cmp,
            "burst_rule": f"경기 시간 {BURST_SEC // 60}분 안에 같은 팀 실책 {BURST_N}개 이상", "rule": RULE}


SUB_KO = {"bad pass": "패스 미스", "lost ball": "볼 뺏김", "out-of-bounds": "라인 아웃", "offensive foul": "공격자 파울",
          "traveling": "트래블링", "shot clock": "샷클락", "step": "라인 밟음", "3-second": "3초", "discontinued dribble": "더블 드리블",
          "palming": "팜잉", "backcourt": "하프라인", "kicked ball": "킥볼", "offensive goaltending": "공격 골텐딩",
          "5-second": "5초", "8-second": "8초", "lane violation": "레인 바이올레이션", "inbound": "인바운드"}


def _kind_ko(e: dict) -> str:
    s = SUB_KO.get(e["sub"], e["sub"] or "기타")
    if e["sub"] == "out-of-bounds" and e["desc"]:
        s += f"({SUB_KO.get(e['desc'], e['desc'])})"
    return s


# ---------------------------------------------------------------- 라이브

def live_summary(acts: list[dict], home: str, away: str) -> dict:
    A = analyze(acts, home, away)
    return {"teams": A["teams"], "rule": RULE}


def burst_alerts(acts: list[dict], home: str, away: str, focus: str) -> list[dict]:
    """경기 중 focus 팀이 3분 안에 실책 3개 이상 → 알림 (몰림 구간 하나에 한 번, 시작 실책 기준 키)."""
    A = analyze(acts, home, away)
    out = []
    for grp in bursts(A["events"], focus):
        third = grp[BURST_N - 1]
        a = acts[third["i"]]
        opp_pts = sum(e["opp_pts"] for e in grp)
        out.append({"key": f"tov-burst:{acts[grp[0]['i']].get('actionNumber', grp[0]['i'])}", "kind": "turnover",
                    "t": third["t"], "label": f"{T.period_label(a['period'])} {T.fmt_clock(T.clock_sec(a['clock']))}",
                    "team": focus, "title": f"{focus} 실책 몰림: {round(third['t'] - grp[0]['t'])}초 사이 {BURST_N}개",
                    "body": f"{span_label(grp[0]['label'], third['label'])} · {', '.join(e['player'] for e in grp[:BURST_N])}"
                            f" · 상대 실책 기반 {opp_pts}점 · {third['score']}"})
    return out


class TurnoverWatch:
    """라이브 Tracker 알림 훅: 닉스 실책 몰림 (기존 빌런 알림과 같은 경로로 Mac 알림)."""

    def __init__(self, focus: str):
        self.focus = focus

    def __call__(self, raw: dict, state: dict, tracker) -> list[dict]:
        pbp = raw["endpoints"].get("playbyplay") or {}
        if raw.get("format") != "live" or not pbp.get("ok") or state.get("status") not in ("live", "final"):
            return []
        return burst_alerts(pbp["data"]["actions"], state["home"], state["away"], self.focus)


class Hooks:
    """알림 훅 여러 개를 차례로 부른다. 하나가 실패해도 나머지는 계속."""

    def __init__(self, *hooks):
        self.hooks = [h for h in hooks if h]

    def __call__(self, raw, state, tracker) -> list[dict]:
        out = []
        for h in self.hooks:
            try:
                out += h(raw, state, tracker)
            except Exception as exc:
                print(f"  ! 알림 훅 실패 ({type(h).__name__}): {exc}", flush=True)
        return out
