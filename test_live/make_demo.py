"""데모 모드(app.py --demo) 데이터 만들기: replay_data/ 에서 재생 가능한 최근 2경기를 고르고,
경기 후 분석 기준값 사본과 함께 test_live/demo/ 에 저장한다.

  python3 test_live/make_demo.py        (데모 경기를 바꾸고 싶을 때 다시 실행)

- 재생 가능 = 같은 출처의 박스스코어·플레이바이플레이가 둘 다 있고 경기가 끝난 것
  (NBA.com live_boxscore/live_playbyplay 또는 ESPN espn_boxscore/espn_playbyplay)
- 경기 정보(날짜·장소·구분)와 '이 경기까지 닉스가 치른 정규시즌 경기 수'(분석 기준값 고르기용)는
  저장된 NBA 공식 일정(briefing/cache/schedule_*.json)에서 가져온다
- 기준값: league/league_totals_*.json · league_misc_*.json (stats.nba.com 리그 팀 합계)을 그대로 복사
- 외부 요청 없음. replay_data/ · briefing/ · league/ 는 읽기만 한다.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPLAY_DATA = os.path.join(HERE, "replay_data")
DEMO_DIR = os.path.join(HERE, "demo")
N_GAMES = 2   # [지난 경기] [전전 경기]


def _load(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def season_of(tipoff_utc: str) -> str:
    d = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def prev_season(season: str) -> str:
    y = int(season[:4]) - 1
    return f"{y}-{(y + 1) % 100:02d}"


def playable(gid: str) -> str | None:
    """재생·분석할 수 있으면 데이터 출처(NBA.com / ESPN), 아니면 None."""
    d = os.path.join(REPLAY_DATA, gid)
    for prefix, source in (("live_", "NBA.com"), ("espn_", "ESPN")):
        box, pbp = _load(os.path.join(d, f"{prefix}boxscore.json")), _load(os.path.join(d, f"{prefix}playbyplay.json"))
        if not box or not pbp or box.get("gameStatus") != 3 or not pbp.get("actions"):
            continue
        # NBA.com 문자중계는 경기 끝 표시가 있어야 한다 (없으면 경기 후 분석이 다시 받으려 한다)
        if prefix == "live_" and not any(a.get("actionType") == "game" for a in pbp["actions"]):
            continue
        return source
    return None


def main() -> None:
    schedule = []
    for path in glob.glob(os.path.join(HERE, "briefing", "cache", "schedule_*.json")):
        schedule += (_load(path) or {}).get("games", [])
    by_id = {g["game_id"]: g for g in schedule}

    found = []
    for gid in os.listdir(REPLAY_DATA):
        source = playable(gid)
        if not source:
            continue
        if gid not in by_id:
            print(f"  · {gid}: 일정에 없어 건너뜀")
            continue
        found.append((by_id[gid], source))
    found.sort(key=lambda x: x[0]["tipoff_utc"], reverse=True)
    if len(found) < N_GAMES:
        raise SystemExit(f"재생 가능한 경기가 {len(found)}개뿐입니다 (필요 {N_GAMES}개)")

    games, seasons = [], set()
    for g, source in found[:N_GAMES]:
        season = season_of(g["tipoff_utc"])
        team_games = sum(1 for x in schedule if x["game_id"][:3] == "002" and x["status"] == 3
                         and season_of(x["tipoff_utc"]) == season and x["tipoff_utc"] <= g["tipoff_utc"])
        games.append({**g, "status": 3, "status_text": "Final", "team_games": team_games, "data_source": source})
        seasons |= {season, prev_season(season)}

    os.makedirs(DEMO_DIR, exist_ok=True)
    copied = []
    for season in sorted(seasons):
        for kind in ("league_totals", "league_misc"):
            src = os.path.join(HERE, "league", f"{kind}_{season}.json")
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(DEMO_DIR, os.path.basename(src)))
                copied.append(os.path.basename(src))
    out = {"made_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "note": "make_demo.py 가 만든 데모 경기 목록 (최신이 앞)", "games": games}
    with open(os.path.join(DEMO_DIR, "games.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    for g in games:
        print(f"  ✓ {g['tipoff_utc'][:10]} {g['away']} @ {g['home']} ({g['game_id']}, {g['data_source']}, 닉스 {g['team_games']}경기째)")
    print(f"  ✓ 기준값 사본: {', '.join(copied) or '없음'}")
    print(f"  → {DEMO_DIR}")


if __name__ == "__main__":
    main()
