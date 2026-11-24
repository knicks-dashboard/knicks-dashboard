"""stats.nba.com PlayByPlayV3 + BoxScoreTraditionalV3 → 공통 형식 (리플레이용)."""
from __future__ import annotations

import re

from .. import model as m

_SUB = re.compile(r"SUB: (.+?) FOR (.+)$")
_AST = re.compile(r"\((.+?) \d+ AST\)")
_REB_COUNTS = re.compile(r"\(Off:(\d+) Def:(\d+)\)")
_NON_PERSONAL = ("Technical", "Defense 3 Second", "Delay")


def build_meta(game_id: str, box: dict) -> m.GameMeta:
    b = box["boxScoreTraditional"]

    def team(t: dict) -> m.Team:
        players = [
            m.Player(id=p["personId"], name=f"{p['firstName']} {p['familyName']}".strip(),
                     family_name=p["familyName"], name_i=p["nameI"], jersey=p.get("jerseyNum", ""),
                     starter=bool(p.get("position")))
            for p in t["players"]
        ]
        return m.Team(id=t["teamId"], tricode=t["teamTricode"], city=t["teamCity"],
                      name=t["teamName"], players=players)

    return m.GameMeta(game_id=game_id, title=m.playoff_title(game_id),
                      home=team(b["homeTeam"]), away=team(b["awayTeam"]))


def build_events(meta: m.GameMeta, pbp: dict) -> list[m.Event]:
    team_ids = {meta.home.id: meta.home.tricode, meta.away.id: meta.away.tricode}
    reb_counts: dict[int, tuple[int, int]] = {}
    last_miss_team: str | None = None
    events: list[m.Event] = []

    for seq, a in enumerate(pbp["game"]["actions"]):
        kind_raw, sub, desc = a["actionType"], a["subType"] or "", a["description"] or ""
        tri = a["teamTricode"] or team_ids.get(a["teamId"])
        pid = a["personId"] or None
        if pid in team_ids or pid == 0:
            pid = None  # 팀 단위 기록
        ev = m.Event(seq=seq, kind=m.OTHER, period=a["period"], clock=m.parse_iso_clock(a["clock"]),
                     team=tri, player_id=pid, description=desc,
                     score_home=_int(a["scoreHome"]), score_away=_int(a["scoreAway"]),
                     distance=a.get("shotDistance") or None,
                     x=a.get("xLegacy"), y=a.get("yLegacy"))

        if kind_raw == "period":
            ev.kind, ev.team = (m.PERIOD_START if sub == "start" else m.PERIOD_END), None
        elif kind_raw in ("Made Shot", "Missed Shot"):
            ev.kind, ev.made, ev.value = m.SHOT, kind_raw == "Made Shot", a["shotValue"]
            if ev.made:
                ast = _AST.search(desc)
                ev.assist_id = meta.team(tri).player_by_name(ast.group(1)) if ast else None
            else:
                last_miss_team = tri
        elif kind_raw == "Free Throw":
            ev.kind, ev.made, ev.value = m.FREE_THROW, not desc.startswith("MISS"), 1
            if not ev.made:
                last_miss_team = tri
        elif kind_raw == "Rebound":
            ev.kind = m.REBOUND
            counts = _REB_COUNTS.search(desc)
            if pid and counts:
                off, dfn = int(counts.group(1)), int(counts.group(2))
                prev = reb_counts.get(pid, (0, 0))
                ev.offensive = off > prev[0]
                reb_counts[pid] = (off, dfn)
            else:
                ev.team_rebound, ev.player_id = True, None
                ev.offensive = tri == last_miss_team
        elif kind_raw == "Turnover":
            ev.kind = m.TURNOVER
        elif kind_raw == "" and "STEAL" in desc:
            ev.kind = m.STEAL
        elif kind_raw == "" and "BLOCK" in desc:
            ev.kind = m.BLOCK
        elif kind_raw == "Foul":
            ev.kind, ev.personal = m.FOUL, not any(k in sub for k in _NON_PERSONAL)
        elif kind_raw == "Substitution":
            hit = _SUB.match(desc)
            ev.kind, ev.out_id = m.SUB, pid
            ev.player_id = meta.team(tri).player_by_name(hit.group(1)) if hit else None
        elif kind_raw == "Timeout":
            ev.kind = m.TIMEOUT
        elif kind_raw == "Jump Ball":
            ev.kind = m.JUMP_BALL
        elif kind_raw == "Violation":
            ev.kind = m.VIOLATION
        events.append(ev)
    return events


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
