"""ESPN 실시간 API → cdn.nba.com live 형식으로 바꾸는 대체 경로 (cdn 이 막혔을 때).

순서: cdn.nba.com → ESPN → stats.nba.com(경기 중엔 점수만). AutoSource 가 매 폴링 cdn 을 먼저 시도하므로
cdn 이 다시 열리면 자동으로 cdn 으로 돌아간다.

- 요청 하나(summary?event=)로 플레이바이플레이·박스스코어·점수를 함께 받는다. nba_http 세션(백오프 포함)을 쓴다.
- 결과는 cdn live 형식(scoreboard / boxscore / playbyplay)으로 바꿔 돌려준다. 그래서 점수 흐름·플레이·선수 기록·
  팀 기록·슛 위치·로테이션·실책 비교·어시스트 네트워크·빌런 알림이 그대로 동작한다.
- 선수 ID: ESPN 선수 ID 는 NBA personId 와 다르다. 기존 로스터(rivals_cache/roster_<팀>_<시즌>.json, NBA 공식)에서
  이름으로 찾아 NBA ID 로 바꾼다 (악센트·Jr. 등 정리 → 성+이름 첫 글자 → 성만이 한 명일 때). 못 찾으면 음수 ESPN ID.
- ESPN 에 없는 것: 속공(fastbreak) 표시. 그래서 '라이브볼 뒤 속공' 은 ESPN 을 쓰는 동안 계산하지 않는다.
- 슛 좌표: ESPN 은 림 기준 피트(x 0~50, 림 x=25, y=림에서 떨어진 거리) → cdn 의 xLegacy/yLegacy(0.1피트, 림 중심 원점).
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import nba_http as NH

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_TO_NBA = {"NY": "NYK", "WSH": "WAS", "SA": "SAS", "GS": "GSW", "NO": "NOP", "UTAH": "UTA"}
_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")
_FT = re.compile(r"(\d) of (\d)")


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’`-]", " ", s)
    s = _SUFFIX.sub(" ", s)
    return " ".join(s.split())


def nba_tri(abbr: str) -> str:
    return ESPN_TO_NBA.get(abbr, abbr)


def _iso_clock(disp: str) -> str:
    """'2:52' → 'PT02M52.00S', '18.2' → 'PT00M18.20S'."""
    disp = (disp or "0").strip()
    if ":" in disp:
        m, s = disp.split(":")
        return f"PT{int(m):02d}M{float(s):05.2f}S"
    return f"PT00M{float(disp):05.2f}S"


def _mins(v: str) -> str:
    try:
        m = int(v)
    except (TypeError, ValueError):
        return "PT00M00.00S"
    return f"PT{m:02d}M00.00S"


def _int(v) -> int:
    try:
        return int(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return 0


def _pair(v: str) -> tuple[int, int]:
    try:
        a, b = str(v).split("-")
        return int(a), int(b)
    except ValueError:
        return 0, 0


class PlayerMap:
    """ESPN 선수 → NBA personId. 로스터 기준, 안 맞으면 이름으로 보조 매칭."""

    def __init__(self, rosters: dict[str, list[dict]]):
        self.full: dict[tuple[str, str], int] = {}
        self.last: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for tri, rows in rosters.items():
            for r in rows:
                n = norm(r["name"])
                self.full[(tri, n)] = r["id"]
                parts = n.split()
                if parts:
                    self.last.setdefault((tri, parts[-1]), []).append((parts[0], r["id"]))
        self.cache: dict[str, int] = {}
        self.unmatched: dict[str, str] = {}
        self.how: dict[str, str] = {}

    def pid(self, espn_id: str, display: str, tri: str) -> int:
        if espn_id in self.cache:
            return self.cache[espn_id]
        n = norm(display)
        pid, how = self.full.get((tri, n)), "로스터 이름"
        if pid is None and n.split():
            first, last = n.split()[0], n.split()[-1]
            cands = self.last.get((tri, last), [])
            same_initial = [p for f, p in cands if f[:1] == first[:1]]
            if len(same_initial) == 1:
                pid, how = same_initial[0], "성+이름 첫 글자"
            elif len(cands) == 1:
                pid, how = cands[0][1], "성"
        if pid is None:   # 로스터에 없는 선수(투웨이·최근 영입 등): 음수 ESPN ID 로 따로 구분
            pid, how = -_int(espn_id), "매칭 안 됨"
            self.unmatched[espn_id] = f"{display} ({tri})"
        self.cache[espn_id], self.how[espn_id] = pid, how
        return pid


EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "espn_events.json")


def find_event(home: str, away: str, tipoff_utc: str, purpose: str | None = None) -> str:
    """ESPN 스코어보드(미 동부 날짜)에서 같은 두 팀 경기의 ESPN 이벤트 ID. 한 번 찾으면 파일에 저장 (경기마다 한 번만 묻는다)."""
    import json
    key = f"{away}@{home}:{tipoff_utc[:10]}"
    try:
        with open(EVENTS_FILE) as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    if key in known:
        return known[key]
    ev_id = _find_event(home, away, tipoff_utc, purpose)
    known[key] = ev_id
    NH._write(EVENTS_FILE, json.dumps(known, indent=1))
    return ev_id


def _find_event(home: str, away: str, tipoff_utc: str, purpose: str | None = None) -> str:
    tip = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    for d in ((tip - timedelta(hours=4)).date(), (tip - timedelta(hours=4)).date() + timedelta(days=1)):
        sb = NH.get_json(f"{SITE}/scoreboard", params={"dates": d.strftime("%Y%m%d")}, purpose=purpose)
        for ev in sb.get("events", []):
            comp = ev["competitions"][0]["competitors"]
            tris = {c["homeAway"]: nba_tri(c["team"]["abbreviation"]) for c in comp}
            if tris.get("home") == home and tris.get("away") == away:
                return ev["id"]
    raise LookupError(f"ESPN 일정에서 {away} @ {home} 경기를 찾지 못함")


# ---------------------------------------------------------------- 플레이 한 줄 → cdn live action

def _turnover_sub(t: str) -> tuple[str, str]:
    """ESPN 종류 이름 → (subType, descriptor). 예: 'Out of Bounds - Bad Pass Turnover' → ('out-of-bounds', 'bad pass')."""
    s = " ".join(t.lower().replace("turnover", " ").split())
    if s.startswith("out of bounds"):
        return "out-of-bounds", s.split("-", 1)[1].strip() if "-" in s else ""
    s = re.sub(r"(\d) second", r"\1-second", s)
    return s or "turnover", ""


def convert_play(p: dict, team_of: dict[str, str], players: PlayerMap, short: dict[str, str], seq: int) -> list[dict]:
    t = (p.get("type") or {}).get("text", "").replace("\n", " ")
    tl = t.lower()
    text = (p.get("text") or "").replace("\n", " ")
    tri = team_of.get(str((p.get("team") or {}).get("id")), "")
    parts = [x["athlete"]["id"] for x in p.get("participants", []) if x.get("athlete")]
    names = {pid: short.get(pid, "") for pid in parts}

    def pid(i):
        if len(parts) <= i:
            return 0
        return players.pid(parts[i], players_name.get(parts[i], ""), team_of_player.get(parts[i], tri))

    base = {"actionNumber": seq, "orderNumber": seq, "clock": _iso_clock((p.get("clock") or {}).get("displayValue")),
            "period": (p.get("period") or {}).get("number", 1), "teamTricode": tri,
            "scoreHome": str(p.get("homeScore", 0)), "scoreAway": str(p.get("awayScore", 0)),
            "description": text, "timeActual": p.get("wallclock"), "qualifiers": [], "subType": "", "descriptor": None,
            "personId": pid(0), "playerNameI": names.get(parts[0], "") if parts else "", "espnType": t}
    c = p.get("coordinate") or {}
    if p.get("shootingPlay") and "free throw" in tl:
        m = _FT.search(t) or _FT.search(text)
        sub = f"{m.group(1)} of {m.group(2)}" if m else ""
        if "technical" in tl:
            sub = "technical"
        return [{**base, "actionType": "freethrow", "subType": sub, "shotResult": "Made" if p.get("scoringPlay") else "Missed"}]
    if p.get("shootingPlay"):
        made = bool(p.get("scoringPlay"))
        a = {**base, "actionType": "3pt" if p.get("pointsAttempted") == 3 else "2pt", "subType": t,
             "shotResult": "Made" if made else "Missed", "isFieldGoal": 1}
        if abs(c.get("x", 1e9)) < 1000 and abs(c.get("y", 1e9)) < 1000:
            a["xLegacy"], a["yLegacy"] = round((c["x"] - 25) * 10), round(c["y"] * 10)
        if made and len(parts) > 1 and "assist" in text.lower():
            a["assistPersonId"] = pid(1)
            a["assistPlayerNameInitial"] = names.get(parts[1], "")
        if not made and len(parts) > 1 and " blocks " in text.lower():
            a["blockPersonId"] = pid(1)
        return [a]
    if "rebound" in tl:
        sub = "offensive" if "offensive" in tl else "defensive"
        return [{**base, "actionType": "rebound", "subType": sub, "qualifiers": [] if parts else ["team"]}]
    if "turnover" in tl or "turnover" in text.lower() or tl == "traveling":
        sub, desc = _turnover_sub(t)
        a = {**base, "actionType": "turnover", "subType": sub, "descriptor": desc or None,
             "qualifiers": [] if parts else ["team"]}
        if len(parts) > 1 and "steal" in text.lower():
            a["stealPersonId"] = pid(1)
        return [a]
    if "foul" in tl or tl == "offensive charge":
        sub = "technical" if "technical" in tl else "offensive" if ("offensive" in tl or "charge" in tl) else "personal"
        desc = None
        m = re.search(r"flagrant.*?type\s*(\d)", tl) or re.search(r"flagrant\s*(\d)", tl)
        if m:
            desc = f"flagrant-type-{m.group(1)}"
        return [{**base, "actionType": "foul", "subType": sub, "descriptor": desc}]
    if tl == "substitution" and len(parts) >= 2:   # [들어온 선수, 나간 선수] → cdn 처럼 out, in 두 줄
        out_a = {**base, "actionType": "substitution", "subType": "out", "personId": pid(1),
                 "playerNameI": names.get(parts[1], "")}
        in_a = {**base, "actionType": "substitution", "subType": "in", "orderNumber": seq + 0.5}
        return [out_a, in_a]
    if tl.startswith("jumpball") or tl.startswith("jump ball"):
        return [{**base, "actionType": "jumpball", "subType": "recovered", "personId": pid(2) or pid(0)}]
    if tl == "end period":
        return [{**base, "actionType": "period", "subType": "end"}]
    if tl in ("end game", "end of game"):
        return [{**base, "actionType": "game", "subType": "end"}]
    if "timeout" in tl:
        return [{**base, "actionType": "timeout"}]
    if "ejection" in tl:
        return [{**base, "actionType": "ejection"}]
    if "violation" in text.lower() or tl in ("kicked ball", "delay of game", "lane", "jump ball violation"):
        return [{**base, "actionType": "violation", "subType": tl}]
    return [{**base, "actionType": tl or "unknown"}]


# convert_play 안에서 쓰는 선수 정보 (convert() 가 채운다)
players_name: dict[str, str] = {}
team_of_player: dict[str, str] = {}


def convert(summary: dict, game_id: str, players: PlayerMap) -> dict:
    """ESPN summary → {scoreboard, boxscore, playbyplay} (cdn live 형식)."""
    comp = summary["header"]["competitions"][0]
    team_of, score, side = {}, {}, {}
    for c in comp["competitors"]:
        tri = nba_tri(c["team"]["abbreviation"])
        team_of[str(c["team"]["id"])] = tri
        score[tri] = _int(c.get("score"))
        side[c["homeAway"]] = tri
    home, away = side["home"], side["away"]
    st = comp["status"]
    state = st["type"]["state"]
    status = {"pre": 1, "in": 2, "post": 3}.get(state, 2)
    short: dict[str, str] = {}
    box_players: dict[str, list[dict]] = {home: [], away: []}
    for t in summary.get("boxscore", {}).get("players", []):
        tri = nba_tri(t["team"]["abbreviation"])
        sb = t["statistics"][0] if t.get("statistics") else {"keys": [], "athletes": []}
        keys = sb.get("keys", [])
        for a in sb.get("athletes", []):
            ath = a["athlete"]
            eid = str(ath["id"])
            players_name[eid], team_of_player[eid], short[eid] = ath.get("displayName", ""), tri, ath.get("shortName", "")
            v = dict(zip(keys, a.get("stats") or []))
            fg, fg3, ft = _pair(v.get("fieldGoalsMade-fieldGoalsAttempted")), \
                _pair(v.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted")), _pair(v.get("freeThrowsMade-freeThrowsAttempted"))
            mins = _int(v.get("minutes"))
            played = not a.get("didNotPlay") and (mins > 0 or bool(a.get("stats")))
            box_players[tri].append({
                "personId": players.pid(eid, ath.get("displayName", ""), tri), "espnId": eid,
                "name": ath.get("displayName", ""), "nameI": ath.get("shortName", ""),
                "familyName": (ath.get("displayName", "").split() or [""])[-1],
                "starter": "1" if a.get("starter") else "0", "played": "1" if played else "0", "oncourt": "0",
                "comment": a.get("reason", "") if not played else "",
                "statistics": {"points": _int(v.get("points")), "reboundsTotal": _int(v.get("rebounds")),
                               "reboundsOffensive": _int(v.get("offensiveRebounds")), "reboundsDefensive": _int(v.get("defensiveRebounds")),
                               "assists": _int(v.get("assists")), "minutes": _mins(v.get("minutes")),
                               "plusMinusPoints": _int(v.get("plusMinus")),
                               "fieldGoalsMade": fg[0], "fieldGoalsAttempted": fg[1],
                               "threePointersMade": fg3[0], "threePointersAttempted": fg3[1],
                               "freeThrowsMade": ft[0], "freeThrowsAttempted": ft[1],
                               "steals": _int(v.get("steals")), "blocks": _int(v.get("blocks")),
                               "turnovers": _int(v.get("turnovers")), "foulsPersonal": _int(v.get("fouls"))}})
    team_stats = {}
    for t in summary.get("boxscore", {}).get("teams", []):
        tri = nba_tri(t["team"]["abbreviation"])
        v = {s["name"]: s.get("displayValue") for s in t.get("statistics", [])}
        fg, fg3, ft = _pair(v.get("fieldGoalsMade-fieldGoalsAttempted")), \
            _pair(v.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted")), _pair(v.get("freeThrowsMade-freeThrowsAttempted"))
        team_stats[tri] = {"points": score.get(tri, 0), "fieldGoalsMade": fg[0], "fieldGoalsAttempted": fg[1],
                           "threePointersMade": fg3[0], "threePointersAttempted": fg3[1],
                           "freeThrowsMade": ft[0], "freeThrowsAttempted": ft[1],
                           "reboundsOffensive": _int(v.get("offensiveRebounds")), "reboundsDefensive": _int(v.get("defensiveRebounds")),
                           "reboundsTotal": _int(v.get("totalRebounds")), "assists": _int(v.get("assists")),
                           "steals": _int(v.get("steals")), "blocks": _int(v.get("blocks")),
                           "turnovers": _int(v.get("turnovers")), "turnoversTeam": _int(v.get("teamTurnovers")),
                           "turnoversTotal": _int(v.get("totalTurnovers") or v.get("turnovers")),
                           "pointsFastBreak": _int(v.get("fastBreakPoints")),
                           "_tov_points_allowed": _int(v.get("turnoverPoints"))}
    # ESPN turnoverPoints = 그 팀 실책으로 상대가 낸 점수 → NBA pointsFromTurnovers(상대 실책으로 낸 점수)는 상대 값
    for tri, opp in ((home, away), (away, home)):
        if tri in team_stats and opp in team_stats:
            team_stats[tri]["pointsFromTurnovers"] = team_stats[opp]["_tov_points_allowed"]
    for v in team_stats.values():
        v.pop("_tov_points_allowed", None)
    acts: list[dict] = []
    for i, p in enumerate(summary.get("plays", [])):
        acts += convert_play(p, team_of, players, short, _int(p.get("sequenceNumber")) or i + 1)
    # 코트 위 5명: 선발 → 교체 (cdn 라이브 박스의 oncourt 와 같은 뜻)
    on = {tri: {p["personId"] for p in box_players[tri] if p["starter"] == "1"} for tri in (home, away)}
    for a in acts:
        if a["actionType"] == "substitution" and a["teamTricode"] in on:
            (on[a["teamTricode"]].add if a["subType"] == "in" else on[a["teamTricode"]].discard)(a["personId"])
    for tri in (home, away):
        for p in box_players[tri]:
            p["oncourt"] = "1" if p["personId"] in on[tri] and status == 2 else "0"
    clock = _iso_clock(st.get("displayClock")) if status == 2 else "PT00M00.00S"
    tip = summary["header"]["competitions"][0].get("date")
    box = {"gameId": game_id, "gameStatus": status, "gameStatusText": st["type"].get("shortDetail", ""),
           "period": st.get("period") or 0, "gameClock": clock, "gameTimeUTC": tip,
           "homeTeam": {"teamTricode": home, "score": score[home], "statistics": team_stats.get(home, {}), "players": box_players[home]},
           "awayTeam": {"teamTricode": away, "score": score[away], "statistics": team_stats.get(away, {}), "players": box_players[away]}}
    sb = {"gameId": game_id, "gameStatus": status, "gameStatusText": box["gameStatusText"], "period": box["period"],
          "gameClock": clock, "gameTimeUTC": tip,
          "homeTeam": {"teamTricode": home, "score": score[home]}, "awayTeam": {"teamTricode": away, "score": score[away]}}
    return {"scoreboard": sb, "boxscore": box, "playbyplay": {"gameId": game_id, "actions": acts}}


class EspnSource:
    """AutoSource 의 두 번째 경로. fetch() 는 다른 소스와 같은 모양 ({source, format: live, endpoints})."""
    name, fmt = "ESPN", "live"

    def __init__(self, game_id: str, home: str, away: str, tipoff_utc: str):
        self.game_id, self.home, self.away, self.tipoff = game_id, home, away, tipoff_utc
        self.event_id: str | None = None
        self.players: PlayerMap | None = None
        self.match_note: dict | None = None
        self.last_summary: dict | None = None
        self.last_summary_at = 0.0
        self.summary_ms: int | None = None

    def _rosters(self) -> dict[str, list[dict]]:
        import rivals as RV
        season = RV._season_of(datetime.fromisoformat(self.tipoff.replace("Z", "+00:00")))
        out = {}
        for tri in (self.home, self.away):
            try:
                out[tri] = RV._roster(tri, season)
            except Exception as exc:   # 로스터를 못 받으면 이름 매칭 없이 (음수 ID)
                print(f"  ! ESPN 선수 매칭용 {tri} 로스터 없음: {exc}", flush=True)
                out[tri] = []
        return out

    def fetch(self) -> dict:
        t0 = datetime.now(timezone.utc)

        def fail(err: str) -> dict:
            r = {"ok": False, "data": None, "error": err, "ms": int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)}
            return {"source": self.name, "format": self.fmt, "endpoints": {"scoreboard": r, "boxscore": dict(r), "playbyplay": dict(r)}}
        try:
            if self.event_id is None:
                self.event_id = find_event(self.home, self.away, self.tipoff, purpose="live")
            if self.players is None:
                self.players = PlayerMap(self._rosters())
            t1 = time.monotonic()
            summary = NH.get_json(f"{SITE}/summary", params={"event": self.event_id}, purpose="live")
            self.summary_ms = int((time.monotonic() - t1) * 1000)   # 응답 시간 (3초 넘으면 갱신 간격을 늘린다)
            self.last_summary, self.last_summary_at = summary, time.time()   # 부상 정보도 여기서 (따로 묻지 않게)
            data = convert(summary, self.game_id, self.players)
        except Exception as exc:
            return fail(f"{type(exc).__name__}: {str(exc)[:160]}")
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        eps = {k: {"ok": True, "data": v, "error": None, "ms": ms} for k, v in data.items()}
        if not data["playbyplay"]["actions"] and data["scoreboard"]["gameStatus"] != 1:
            eps["playbyplay"] = {"ok": False, "data": None, "error": "ESPN 플레이 기록이 비어 있음", "ms": ms}
        self.match_note = {"matched": sum(1 for h in self.players.how.values() if h != "매칭 안 됨"),
                           "unmatched": sorted(self.players.unmatched.values()),
                           "by": {k: sum(1 for h in self.players.how.values() if h == k) for k in ("로스터 이름", "성+이름 첫 글자", "성")}}
        return {"source": self.name, "format": self.fmt, "endpoints": eps, "espn_event": self.event_id,
                "espn_match": self.match_note, "espn_ms": self.summary_ms}


# ---------------------------------------------------------------- 끝난 경기 (리플레이·지난 경기 브리핑·경기 후 분석의 대체 소스)

def game_info(game_id: str) -> dict | None:
    """저장된 닉스 일정(briefing/cache/schedule_*.json)에서 홈·원정·팁오프 시각."""
    import glob
    import json
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for path in sorted(glob.glob(os.path.join(here, "briefing", "cache", "schedule_*.json")), reverse=True):
        try:
            with open(path) as f:
                games = json.load(f)["games"]
        except (OSError, ValueError, KeyError):
            continue
        g = next((x for x in games if x.get("game_id") == game_id), None)
        if g:
            return g
    # 닉스 경기가 아니면 리그 전체 일정 (fatigue.py 가 저장해 둔 league/schedule_full_<시즌>.json)
    for path in sorted(glob.glob(os.path.join(here, "league", "schedule_full_*.json")), reverse=True):
        try:
            with open(path) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            continue
        g = next((x for x in rows if x.get("gid") == game_id), None)
        if g:
            return {"game_id": game_id, "home": g["home"], "away": g["away"], "tipoff_utc": g["utc"]}
    return None


def final_game(game_id: str, game: dict | None = None) -> dict:
    """끝난 경기를 ESPN 요약으로 받아 cdn live 형식 {box, pbp, event, match} 로. ESPN 요약은 요청 관리자가 영구 저장한다."""
    g = game or game_info(game_id)
    if not g:
        raise LookupError(f"{game_id} 경기 정보(홈·원정·날짜)를 일정에서 찾지 못함")
    src = EspnSource(game_id, g["home"], g["away"], g["tipoff_utc"])
    src.event_id = find_event(g["home"], g["away"], g["tipoff_utc"])
    src.players = PlayerMap(src._rosters())
    summary = NH.get_json(f"{SITE}/summary", params={"event": src.event_id})
    data = convert(summary, game_id, src.players)
    if data["boxscore"]["gameStatus"] != 3:
        raise RuntimeError("ESPN 에서도 아직 경기가 끝나지 않음")
    NH.note_fallback("cdn.nba.com", "ESPN")
    return {"box": data["boxscore"], "pbp": data["playbyplay"], "event": src.event_id,
            "match": {"unmatched": sorted(src.players.unmatched.values())}}
