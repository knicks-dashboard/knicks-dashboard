"""리플레이 엔진이 PBP로 다시 계산한 최종 박스스코어를 공식 박스스코어와 대조한다.

python3 validate_replay.py 0042500405
"""
import sys

from gamecast.adapters import stats_v3
from gamecast.fetch import load
from gamecast.lineups import infer_from_events
from gamecast.state import build_snapshot

FIELDS = {"points": "pts", "fieldGoalsMade": "fgm", "fieldGoalsAttempted": "fga",
          "threePointersMade": "fg3m", "threePointersAttempted": "fg3a",
          "freeThrowsMade": "ftm", "freeThrowsAttempted": "fta",
          "reboundsOffensive": "oreb", "reboundsDefensive": "dreb", "assists": "ast",
          "steals": "stl", "blocks": "blk", "turnovers": "tov", "foulsPersonal": "pf",
          "plusMinusPoints": "pm"}


def official_seconds(s: str) -> int:
    if not s:
        return 0
    mm, ss = s.split(":")
    return int(mm) * 60 + int(float(ss))


def main(game_id: str) -> int:
    pbp, box = load(game_id)
    meta = stats_v3.build_meta(game_id, box)
    events = stats_v3.build_events(meta, pbp)
    starters = infer_from_events(meta, events)
    snap = build_snapshot(meta, events, starters, events[-1].elapsed, meta.away.tricode, None)

    issues = 0
    for p, lineup in sorted(starters.items()):
        for tri, ids in lineup.items():
            if len(ids) != 5:
                print(f"[라인업] {tri} P{p} 시작 인원 {len(ids)}명")
                issues += 1

    for side in ("homeTeam", "awayTeam"):
        off = box["boxScoreTraditional"][side]
        tri = off["teamTricode"]
        mine = {r["id"]: r for r in snap["box"][tri]["players"]}
        for p in off["players"]:
            st = p["statistics"]
            r = mine.get(p["personId"], {})
            for of, k in FIELDS.items():
                if int(st[of]) != int(r.get(k, 0)):
                    print(f"[선수] {tri} {p['nameI']:<16} {k}: 공식 {int(st[of])} / 계산 {r.get(k, 0)}")
                    issues += 1
            diff = abs(official_seconds(st["minutes"]) - r.get("sec", 0))
            if diff > 1:
                print(f"[출전시간] {tri} {p['nameI']:<16} 공식 {st['minutes']} / 계산 {r.get('min', '0:00')}")
                issues += 1
        tot = snap["box"][tri]["totals"]
        for of, k in FIELDS.items():
            if k == "pm":
                continue
            if int(off["statistics"][of]) != int(tot[k]):
                print(f"[팀합계] {tri} {k}: 공식 {off['statistics'][of]} / 계산 {tot[k]}")
                issues += 1

    print(f"최종 스코어 계산: {snap['score']}")
    print("불일치 0건 — 공식 박스스코어와 일치" if issues == 0 else f"불일치 {issues}건")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "0042500405"))
