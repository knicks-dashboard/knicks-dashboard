"""리플레이용 원본 데이터 다운로드: python3 -m gamecast.fetch 0042500405"""
from __future__ import annotations

import json
import os
import sys

from nba_api.stats.endpoints import boxscoretraditionalv3, playbyplayv3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES_DIR = os.path.join(ROOT, "data", "games")


def game_dir(game_id: str) -> str:
    return os.path.join(GAMES_DIR, game_id)


def fetch(game_id: str, force: bool = False) -> str:
    d = game_dir(game_id)
    os.makedirs(d, exist_ok=True)
    targets = {
        "pbp_v3.json": lambda: playbyplayv3.PlayByPlayV3(game_id=game_id, timeout=60).get_dict(),
        "box_v3.json": lambda: boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=game_id, timeout=60).get_dict(),
    }
    for name, get in targets.items():
        path = os.path.join(d, name)
        if force or not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(get(), f)
    return d


def load(game_id: str, refresh: bool = False) -> tuple[dict, dict]:
    d = fetch(game_id, force=refresh)
    with open(os.path.join(d, "pbp_v3.json")) as f:
        pbp = json.load(f)
    with open(os.path.join(d, "box_v3.json")) as f:
        box = json.load(f)
    return pbp, box


if __name__ == "__main__":
    print(fetch(sys.argv[1], force="--force" in sys.argv))
