"""스냅샷 위에 얹는 해석 레이어: 알림, 승리 확률, '오늘 평소와 다른 점'."""
from __future__ import annotations

import math

from . import model as m

RUN_ALERT_PTS = 6        # 연속 득점 알림 기준
FOUL_ALERT_PF = 4        # 주전 파울 알림 기준

# 승리 확률: 남은 시간 동안 점수 차가 정규분포로 움직인다고 보는 단순 모델
FULL_GAME_SIGMA = 13.0   # 한 경기 최종 점수 차의 표준편차 (NBA 대략값)
HOME_EDGE = 2.5          # 홈 어드밴티지 (경기 전체 기준, 남은 시간 비율만큼 반영)
LATE_SIGMA_FLOOR = 1.5   # 시간이 남아 있으면 마지막 공격 한 번(최대 3~4점)의 불확실성은 남는다
WP_STEP = 10             # 그래프 샘플 간격(경기 초)

UNUSUAL_MIN_ELAPSED = 6 * 60   # 6분 전에는 표본이 너무 적어 표시하지 않는다
UNUSUAL_MIN_Z = 1.0
UNUSUAL_COUNT = 3
PLAYER_STD_FLOOR = 1.0         # 블록·스틸처럼 분산이 작은 기록이 과하게 튀지 않게

TEAM_STATS = {
    # key: (라벨, 단위, 누적 기록 여부, 낮을수록 좋은지)
    "PTS": ("득점", "점", True, False),
    "OPP_PTS": ("실점", "점", True, True),
    "EFG_PCT": ("eFG%", "%", False, False),
    "FG3_PCT": ("3점 성공률", "%", False, False),
    "FG3A": ("3점 시도", "개", True, False),
    "FTA_RATE": ("자유투 획득률", "%", False, False),
    "TOV_PCT": ("턴오버율", "%", False, True),
    "AST": ("어시스트", "개", True, False),
    "REB": ("리바운드", "개", True, False),
    "OREB": ("공격 리바운드", "개", True, False),
    "TOV": ("턴오버", "개", True, True),
}
PLAYER_STATS = {
    "PTS": ("득점", "점", "pts", False),
    "REB": ("리바운드", "개", "reb", False),
    "AST": ("어시스트", "개", "ast", False),
    "FG3M": ("3점 성공", "개", "fg3m", False),
    "TOV": ("턴오버", "개", "tov", True),
    "PF": ("파울", "개", "pf", True),
    "STL": ("스틸", "개", "stl", False),
    "BLK": ("블록", "개", "blk", False),
}


# ---- 알림 ----

def alerts(meta: m.GameMeta, scoring: list[dict], foul_log: list[dict], players: dict) -> list[dict]:
    out = []
    streak_team, streak_pts, streak_start, current = None, 0, None, None
    for s in scoring:
        if s["team"] != streak_team:
            streak_team, streak_pts, streak_start, current = s["team"], 0, s["label"], None
        streak_pts += s["pts"]
        if streak_pts >= RUN_ALERT_PTS:
            if current is None:
                current = {"id": f"run-{s['t']:.1f}-{streak_team}", "kind": "run", "t": s["t"],
                           "team": streak_team, "label": s["label"], "start_label": streak_start}
                out.append(current)
            current.update(pts=streak_pts, end_label=s["label"],
                           text=f"{streak_team} {streak_pts}-0 런 ({streak_start}부터)")
    if current is not None:
        current["active"] = True

    for f in foul_log:
        tri, p = players[f["player_id"]]
        if not p.starter or f["pf"] < FOUL_ALERT_PF:
            continue
        note = " · 퇴장" if f["pf"] >= 6 else ""
        out.append({"id": f"foul-{p.id}-{f['pf']}", "kind": "foul", "t": f["t"], "team": tri,
                    "label": f["label"], "pf": f["pf"], "severe": f["pf"] >= 5,
                    "text": f"{p.name} 파울 {f['pf']}개{note}"})
    return sorted(out, key=lambda a: a["t"])


# ---- 승리 확률 ----

def win_probability(meta: m.GameMeta, focus: str, scoring: list[dict], now: float) -> dict:
    focus_home = focus == meta.home.tricode
    total = m.REGULATION_PERIODS * m.PERIOD_SEC

    def prob(t: float, margin: int) -> float:
        if t <= total:
            remaining = total - t
        else:  # 연장전: 해당 연장 남은 시간
            remaining = m.elapsed_to_clock(t)[1]
        frac = max(remaining, 0) / total
        if frac <= 0:
            return 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        edge = HOME_EDGE * frac * (1 if focus_home else -1)
        z = (margin + edge) / (FULL_GAME_SIGMA * math.sqrt(frac) + LATE_SIGMA_FLOOR)
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    times = sorted({*range(0, int(now) + 1, WP_STEP), now, *(s["t"] for s in scoring)})
    series, i, margin = [], 0, 0
    for t in times:
        while i < len(scoring) and scoring[i]["t"] <= t:
            margin = scoring[i]["margin"]
            i += 1
        series.append({"t": t, "p": round(prob(t, margin), 4), "margin": margin})
    return {"current": series[-1]["p"] if series else 0.5, "series": series,
            "model": {"sigma": FULL_GAME_SIGMA, "home_edge": HOME_EDGE}}


# ---- 오늘 평소와 다른 점 ----

def unusual(snap: dict, baseline: dict | None, now: float) -> dict:
    if not baseline or "game_dist" not in baseline["team"]:
        return {"items": [], "note": "기준값 분포 데이터가 없습니다."}
    if now < UNUSUAL_MIN_ELAPSED:
        return {"items": [], "note": "경기 시작 6분 후부터 표시합니다."}
    focus, opp = snap["game"]["focus"], snap["game"]["opponent"]
    frac = min(now / (m.REGULATION_PERIODS * m.PERIOD_SEC), 1.0)
    final = snap["status_final"]
    t, o, ff = snap["box"][focus]["totals"], snap["box"][opp]["totals"], snap["four_factors"][focus]
    team_now = {
        "PTS": t["pts"], "OPP_PTS": o["pts"], "EFG_PCT": ff["efg"],
        "FG3_PCT": t["fg3m"] / t["fg3a"] if t["fg3a"] else None,
        "FG3A": t["fg3a"], "FTA_RATE": ff["ftr"], "TOV_PCT": ff["tov"],
        "AST": t["ast"], "REB": t["reb"], "OREB": t["oreb"], "TOV": t["tov"],
    }

    cands = []
    for d in baseline["team"]["game_dist"]:
        key = d["STAT"]
        if key not in TEAM_STATS or team_now.get(key) is None:
            continue
        label, unit, counting, lower_better = TEAM_STATS[key]
        cands.append(_candidate(f"{focus} 팀", label, unit, team_now[key], d["MEAN"], d["STD"],
                                counting, lower_better, frac, final))

    played = {p["id"]: p for p in snap["box"][focus]["players"]}
    for d in baseline["players"].get("game_dist", []):
        p = played.get(d["PLAYER_ID"])
        if not p or d["STAT"] not in PLAYER_STATS:
            continue
        label, unit, field, lower_better = PLAYER_STATS[d["STAT"]]
        cands.append(_candidate(p["name"], label, unit, p[field], d["MEAN"], max(d["STD"], PLAYER_STD_FLOOR),
                                True, lower_better, frac, final, subject_id=p["id"]))

    cands.sort(key=lambda c: -abs(c["z"]))
    items, used = [], set()
    for c in cands:
        if abs(c["z"]) < UNUSUAL_MIN_Z:
            break
        sid = c.pop("subject_id")
        if sid is not None and sid in used:  # 한 선수가 여러 칸을 차지하지 않게
            continue
        used.add(sid)
        items.append(c)
        if len(items) == UNUSUAL_COUNT:
            break
    note = "" if items else "기준값에서 크게 벗어난 기록이 아직 없습니다."
    return {"items": items, "note": note}


def _candidate(subject, label, unit, value, mean, std, counting, lower_better, frac, final, subject_id=None):
    projected = value / frac if counting and frac > 0 else value
    # 남은 시간이 많을수록 불확실하므로 sqrt(진행 비율)로 줄인다
    z = (projected - mean) / std * math.sqrt(frac) if std else 0.0
    higher = projected > mean
    good = higher != lower_better
    fmt = (lambda v: f"{v * 100:.1f}%") if unit == "%" else (lambda v: f"{v:.0f}{unit}" if v == int(v) else f"{v:.1f}{unit}")
    value_text = fmt(value)
    if counting and not final:
        value_text += f" (페이스 {projected:.1f}{unit})"
    return {"subject": subject, "label": label, "value_text": value_text, "baseline_text": fmt(round(mean, 3)),
            "z": round(z, 2), "higher": higher, "good": good, "subject_id": subject_id}
