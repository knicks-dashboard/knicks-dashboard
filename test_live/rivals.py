"""한 줄 반응 타임라인 · 오늘의 빌런 카드.

- 내 반응:   test_live/my_reactions.json  (경기별 한 줄 + 이모지, 그 순간의 쿼터·시간·점수)
- 내 메모:   test_live/my_rivals.json     (선수 ID 기준 — 팀을 옮겨도 다음 맞대결에 다시 보인다)
- 공식 기록 캐시: test_live/rivals_cache/  (끝난 경기의 플레이바이플레이는 한 번만 받는다)
모든 쓰기는 test_live/ 안에서만.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import resilient as RS
import tracker as T

HERE = os.path.dirname(os.path.abspath(__file__))
REACTIONS_PATH = os.path.join(HERE, "my_reactions.json")
RIVALS_PATH = os.path.join(HERE, "my_rivals.json")
CACHE = os.path.join(HERE, "rivals_cache")
PBP_CACHE = os.path.join(CACHE, "pbp")

EMOJIS = ("🔥", "😱", "😤", "😂", "👏")
TEXT_MAX = 120
TAG_PRESETS = ("천적", "트래시토커", "플라핑", "거친 수비", "클러치", "닉스 킬러")
VILLAIN_RUN_PTS = 6

# 거친 플레이로 세지 않는 테크니컬 (수비 3초·지연·플라핑 같은 규정 위반성 테크니컬)
NON_ROUGH_TECH = {"defensive-3-second", "delay", "delay-of-game", "flopping", "excess-timeout",
                  "too-many-players", "illegal-defense", "non-unsportsmanlike"}

_lock = threading.Lock()


def _read(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _clean(v):
    """pandas 빈 칸(NaN)은 JSON 에 넣을 수 없어서(브라우저가 못 읽는다) None 으로 바꾼다."""
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def _write(path: str, obj) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(_clean(obj), f, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(tmp, path)   # 쓰는 도중에 꺼져도 파일이 깨지지 않게


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 1) 한 줄 반응

def reactions_for(game_id: str) -> list[dict]:
    data = _read(REACTIONS_PATH, {"reactions": []})
    return [r for r in data["reactions"] if r.get("game_id") == game_id]


def add_reaction(state: dict, game_id: str, mode: str, text: str, emoji: str) -> dict:
    """저장 순간의 쿼터·시간·점수는 서버가 그 채널의 현재 화면 데이터에서 직접 기록한다."""
    if emoji not in EMOJIS:
        raise ValueError("이모지를 골라 주세요")
    text = " ".join((text or "").split())[:TEXT_MAX]
    if state.get("status") not in ("live", "final") or not state.get("period"):
        raise ValueError("경기 중에만 기록할 수 있습니다")
    focus, opp = state["focus"], state["opponent"]
    sc = state.get("score", {})
    period, clock = state["period"], state.get("clock", "")
    r = {"id": uuid.uuid4().hex[:10], "game_id": game_id, "mode": mode,
         "saved_kst": datetime.now(T.KST).isoformat(timespec="seconds"),
         "emoji": emoji, "text": text,
         "period": period, "period_label": state.get("period_label", ""), "clock": clock,
         "t": T.elapsed(period, T.clock_sec_label(clock)),
         "home": state["home"], "away": state["away"], "focus": focus, "opponent": opp,
         "score": {focus: sc.get(focus, 0), opp: sc.get(opp, 0)},
         "margin": sc.get(focus, 0) - sc.get(opp, 0)}
    with _lock:
        data = _read(REACTIONS_PATH, {"reactions": []})
        data["reactions"].append(r)
        _write(REACTIONS_PATH, data)
    return r


def delete_reaction(rid: str) -> bool:
    with _lock:
        data = _read(REACTIONS_PATH, {"reactions": []})
        n = len(data["reactions"])
        data["reactions"] = [r for r in data["reactions"] if r.get("id") != rid]
        if len(data["reactions"]) != n:
            _write(REACTIONS_PATH, data)
            return True
    return False


# ---------------------------------------------------------------- 2) 내 메모·태그

def rivals_notes() -> dict:
    return _read(RIVALS_PATH, {"players": {}})["players"]


def save_rival_note(pid: int, name: str, team: str, memo: str, tags: list[str]) -> dict | None:
    """메모와 태그가 모두 비면 지운다."""
    memo = (memo or "").strip()[:300]
    tags = [t.strip().lstrip("#")[:20] for t in (tags or []) if t and t.strip().lstrip("#")][:8]
    with _lock:
        data = _read(RIVALS_PATH, {"players": {}})
        key = str(int(pid))
        if not memo and not tags:
            data["players"].pop(key, None)
            _write(RIVALS_PATH, data)
            return None
        old = data["players"].get(key, {})
        data["players"][key] = {"name": name or old.get("name", ""), "team_when_noted": old.get("team_when_noted") or team,
                                "team": team or old.get("team", ""), "memo": memo, "tags": tags,
                                "created_utc": old.get("created_utc") or _now_iso(), "updated_utc": _now_iso()}
        _write(RIVALS_PATH, data)
        return data["players"][key]


# ---------------------------------------------------------------- 3) 빌런 계산 (공식 기록)

def _season_of(d: datetime) -> str:
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def _prev(season: str) -> str:
    y = int(season[:4]) - 1
    return f"{y}-{(y + 1) % 100:02d}"


def _team_id(tri: str) -> int:
    from nba_api.stats.static import teams
    return teams.find_team_by_abbreviation(tri)["id"]


def _cached(name: str, fetch, max_age: timedelta | None):
    """끝난 시즌은 영구, 진행 중인 데이터는 max_age 동안만 캐시."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and (max_age is None or time.time() - os.path.getmtime(path) < max_age.total_seconds()):
        with open(path) as f:
            return json.load(f)
    return RS.fetch(name, fetch, path)   # 실패하면 저장본 + 자동 재시도 (resilient.py)


def _stats_call(fn, tries: int = 3):
    import nba_http as NH
    last = None
    for i in range(tries):
        try:
            return fn()
        except NH.NotSent:
            raise   # 쉬는 중·연결 없음: 다시 시도해도 보내지 않는다
        except Exception as exc:
            last = exc
            if not NH.open_now("stats.nba.com"):
                raise   # 방금 차단(403)·시간 초과로 쉬기 시작했다 → 같은 요청을 거듭 보내지 않는다
            time.sleep(2 + i * 3)
    raise last


def _game_logs(season: str, season_type: str, cur_season: str, **filters) -> list[dict]:
    from nba_api.stats.endpoints import playergamelogs
    key = "_".join(f"{k}-{v}" for k, v in sorted(filters.items()))
    name = f"logs_{season}_{season_type.replace(' ', '')}_{key}.json"

    def fetch():
        df = _stats_call(lambda: playergamelogs.PlayerGameLogs(season_nullable=season, season_type_nullable=season_type,
                                                                timeout=30, **filters).get_data_frames()[0])
        cols = ["PLAYER_ID", "PLAYER_NAME", "TEAM_ABBREVIATION", "GAME_ID", "GAME_DATE", "MATCHUP", "PTS", "MIN"]
        return df[cols].to_dict("records") if len(df) else []
    return _cached(name, fetch, None if season != cur_season else timedelta(hours=6))


def _roster(opp: str, season: str) -> list[dict]:
    from nba_api.stats.endpoints import commonteamroster

    def fetch():
        df = _stats_call(lambda: commonteamroster.CommonTeamRoster(team_id=_team_id(opp), season=season,
                                                                   timeout=30).get_data_frames()[0])
        txt = lambda v: v if isinstance(v, str) else ""
        return [{"id": int(r["PLAYER_ID"]), "name": r["PLAYER"], "pos": txt(r["POSITION"]), "num": txt(r["NUM"])}
                for _, r in df.iterrows()]
    return _cached(f"roster_{opp}_{season}.json", fetch, timedelta(hours=12))


def rough_events(game_id: str) -> list[dict]:
    """한 경기 공식 플레이바이플레이에서 테크니컬·플래그런트·퇴장만 뽑아 저장 (끝난 경기는 다시 받지 않는다)."""
    os.makedirs(PBP_CACHE, exist_ok=True)
    path = os.path.join(PBP_CACHE, f"{game_id}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)["events"]
    d = T.cdn_get(f"playbyplay/playbyplay_{game_id}.json")["game"]
    out = []
    for a in d["actions"]:
        kind = classify(a)
        if kind:
            out.append({"pid": a.get("personId"), "kind": kind, "desc": a.get("description", ""),
                        "label": f"{T.period_label(a['period'])} {T.fmt_clock(T.clock_sec(a['clock']))}"})
    if any(a.get("actionType") == "game" and a.get("subType") == "end" for a in d["actions"]):
        _write(path, {"game_id": game_id, "events": out})
    return out


def classify(a: dict) -> str | None:
    """공식 플레이바이플레이 한 줄 → 'tech' / 'flagrant1' / 'flagrant2' / 'ejection' / None."""
    t, sub, desc = a.get("actionType"), a.get("subType"), a.get("descriptor") or ""
    if not a.get("personId"):
        return None   # 팀 테크니컬
    if t == "ejection":
        return "ejection"
    if t == "foul" and desc.startswith("flagrant-type-2"):
        return "flagrant2"
    if t == "foul" and desc.startswith("flagrant-type-1"):
        return "flagrant1"
    if t == "foul" and sub == "technical" and desc not in NON_ROUGH_TECH:
        return "tech"
    return None


SEASON_TYPES = (("Regular Season", "정규시즌"), ("Playoffs", "플레이오프"))


def rough_record(pid: int, season: str, cur_season: str, progress=None) -> dict:
    games = []
    for st, _ in SEASON_TYPES:
        games += [g["GAME_ID"] for g in _game_logs(season, st, cur_season, player_id_nullable=pid)]
    pre = [g["GAME_ID"] for g in _game_logs(season, "Pre Season", cur_season, player_id_nullable=pid)] if season == cur_season else []
    rec = {"season": season, "games": len(games), "tech": 0, "flagrant": 0, "flagrant2": 0, "ejection": 0, "events": [],
           "missing": 0}

    def tally(target: dict, ids: list[str]):
        for gid in ids:
            try:
                evs = rough_events(gid)
            except Exception:
                target["missing"] += 1
                continue
            for e in evs:
                if e["pid"] != pid:
                    continue
                if e["kind"] == "tech":
                    target["tech"] += 1
                elif e["kind"].startswith("flagrant"):
                    target["flagrant"] += 1
                    target["flagrant2"] += e["kind"] == "flagrant2"
                elif e["kind"] == "ejection":
                    target["ejection"] += 1
                target["events"].append({**e, "game_id": gid})
            if progress:
                progress()
    tally(rec, games)
    if pre:
        rec["preseason"] = {"games": len(pre), "tech": 0, "flagrant": 0, "flagrant2": 0, "ejection": 0, "events": [], "missing": 0}
        tally(rec["preseason"], pre)
    return rec


def compute_villains(opp: str, focus: str = "NYK", progress=None) -> dict:
    cur = _season_of(datetime.now(timezone.utc))
    h2h_seasons = [_prev(cur), _prev(_prev(cur))]        # 지난 2시즌 (예: 2025-26, 2024-25)
    roster = _roster(opp, cur)
    ids = {p["id"] for p in roster}
    focus_id = _team_id(focus)
    agg: dict[int, dict] = {}
    for season in h2h_seasons:
        for st, st_ko in SEASON_TYPES:
            for g in _game_logs(season, st, cur, opp_team_id_nullable=focus_id):
                pid = int(g["PLAYER_ID"])
                if pid not in ids:
                    continue
                a = agg.setdefault(pid, {"games": 0, "pts": 0, "best": None, "log": []})
                a["games"] += 1
                a["pts"] += g["PTS"]
                item = {"date": g["GAME_DATE"][:10], "pts": g["PTS"], "team": g["TEAM_ABBREVIATION"],
                        "season": season, "type": st_ko}
                a["log"].append(item)
                if not a["best"] or g["PTS"] > a["best"]["pts"]:
                    a["best"] = item
    cands = [(pid, a) for pid, a in agg.items() if a["games"] >= 2] or list(agg.items())
    cands.sort(key=lambda x: -x[1]["pts"] / x[1]["games"])
    picks = cands[:1]
    if len(cands) > 1:
        top = cands[0][1]["pts"] / cands[0][1]["games"]
        second = cands[1][1]["pts"] / cands[1][1]["games"]
        if second >= top * 0.8:   # 두 번째도 비슷하게 위협적이면 2명
            picks.append(cands[1])
    by_id = {p["id"]: p for p in roster}
    villains = []
    for pid, a in picks:
        p = by_id[pid]
        villains.append({
            "id": pid, "name": p["name"], "pos": p["pos"], "num": p["num"],
            "games": a["games"], "avg_pts": round(a["pts"] / a["games"], 1), "best": a["best"],
            "log": sorted(a["log"], key=lambda x: x["date"], reverse=True),
            "rough": [{**rough_record(pid, s, cur, progress), "label": lbl}
                      for s, lbl in ((cur, "이번 시즌"), (_prev(cur), "지난 시즌"))],
            "link": f"https://www.nba.com/player/{pid}",
        })
    return {"opponent": opp, "focus": focus, "season": cur, "h2h_seasons": h2h_seasons,
            "villains": villains, "roster": roster, "computed_utc": _now_iso(),
            "basis": f"{opp} 현재 로스터 중 {' · '.join(reversed(h2h_seasons))} 정규시즌·플레이오프 {focus} 상대 평균 득점 1위"
                     f"{' (2경기 이상)' if any(a['games'] >= 2 for _, a in cands) else ''}. 2위가 1위의 80% 이상이면 2명.",
            "sources": [{"name": "NBA.com 선수 경기 기록 (stats.nba.com)", "url": "https://www.nba.com/stats/players/boxscores"},
                        {"name": "NBA.com 공식 플레이바이플레이 집계", "url": "https://www.nba.com/games"}],
            "rough_note": "테크니컬은 수비 3초·지연·플라핑 같은 규정 위반성 테크니컬을 빼고 셌습니다. "
                          "경기 후 리그가 취소·추가한 징계는 반영되지 않습니다. 정규시즌·플레이오프 기준."}


class VillainService:
    """상대 팀별로 백그라운드에서 한 번 계산하고, 하루 동안 디스크 캐시를 쓴다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def get(self, opp: str, focus: str = "NYK") -> dict:
        today = datetime.now(T.KST).strftime("%Y%m%d")
        name = f"villains_{focus}_{opp}_{today}.json"
        with self.lock:
            job = self.jobs.get(opp)
            if job and job.get("day") == today and job["status"] in ("loading", "ready"):
                return job["view"] if job["status"] == "ready" else {"status": "loading", "progress": job["progress"]}
            path = os.path.join(CACHE, name)
            if os.path.exists(path):
                data = _read(path, None)
                if data:
                    self.jobs[opp] = {"day": today, "status": "ready", "view": {"status": "ready", **data}}
                    return self.jobs[opp]["view"]
            if job and job["status"] == "error" and time.time() - job["at"] < 600:
                return {"status": "error", "error": job["error"]}
            job = {"day": today, "status": "loading", "progress": 0, "at": time.time()}
            self.jobs[opp] = job

        def run():
            def tick():
                job["progress"] += 1
            try:
                data = compute_villains(opp, focus, tick)
                _write(os.path.join(CACHE, name), data)
                with self.lock:
                    job.update(status="ready", view={"status": "ready", **data})
                print(f"[빌런] {opp}: {', '.join(v['name'] for v in data['villains']) or '없음'}", flush=True)
            except Exception as exc:
                with self.lock:
                    job.update(status="error", error=RS.friendly("빌런 기록", exc, opp, "10분 뒤 다시 시도"), at=time.time())
                print(f"[빌런] {opp} 계산 실패: {exc}", flush=True)
        threading.Thread(target=run, daemon=True).start()
        return {"status": "loading", "progress": 0}


# ---------------------------------------------------------------- 4) 경기 중 빌런 알림

def villain_alerts(acts: list[dict], villains: dict[int, str], home: str, away: str) -> list[dict]:
    """빌런 혼자 연속 6점 이상(그 사이 아무도 득점하지 않음), 테크니컬·플래그런트·퇴장."""
    out = []
    sh = sa = 0
    run_pid, run_pts, run_start, run_done = None, 0, None, False
    for i, a in enumerate(acts):
        label = f"{T.period_label(a['period'])} {T.fmt_clock(T.clock_sec(a['clock']))}"
        t = T.elapsed(a["period"], T.clock_sec(a["clock"]))
        pid = a.get("personId")
        h, w = T.to_int(a.get("scoreHome")), T.to_int(a.get("scoreAway"))
        if h is not None and w is not None and (h, w) != (sh, sa):
            pts = (h - sh) + (w - sa)
            if pid in villains and pid == run_pid:
                run_pts += pts
            elif pid in villains:
                run_pid, run_pts, run_start, run_done = pid, pts, i, False
            else:
                run_pid, run_pts = None, 0
            sh, sa = h, w
            if run_pid and run_pts >= VILLAIN_RUN_PTS and not run_done:
                run_done = True
                out.append({"key": f"villain-run:{run_pid}:{run_start}", "kind": "villain", "t": t, "label": label,
                            "team": a.get("teamTricode"), "title": f"빌런 {villains[run_pid]} 혼자 연속 {run_pts}점",
                            "body": f"{label} · {away} {sa} - {sh} {home}"})
        kind = classify(a) if pid in villains else None
        if kind:
            what = {"tech": "테크니컬", "flagrant1": "플래그런트 1", "flagrant2": "플래그런트 2", "ejection": "퇴장"}[kind]
            out.append({"key": f"villain-foul:{a.get('actionNumber', i)}", "kind": "villain", "t": t, "label": label,
                        "team": a.get("teamTricode"), "title": f"빌런 {villains[pid]} {what}",
                        "body": f"{label} · {a.get('description', '')}"})
    return out


class VillainWatch:
    """라이브 Tracker 에 붙는 알림 훅. 빌런 명단이 늦게 도착해도 이미 지나간 상황은 Mac 알림을 보내지 않는다."""

    def __init__(self, service: VillainService, opp: str, focus: str):
        self.service, self.opp, self.focus = service, opp, focus
        self.primed = False

    def ids(self) -> dict[int, str]:
        v = self.service.get(self.opp, self.focus)
        if v.get("status") != "ready":
            return {}
        return {x["id"]: x["name"] for x in v["villains"]}

    def __call__(self, raw: dict, state: dict, tracker) -> list[dict]:
        pbp = raw["endpoints"].get("playbyplay") or {}
        if raw.get("format") != "live" or not pbp.get("ok") or state.get("status") not in ("live", "final"):
            return list(getattr(self, "last", []))
        vids = self.ids()
        if not vids:
            return []
        alerts = villain_alerts(pbp["data"]["actions"], vids, state["home"], state["away"])
        if not self.primed:
            self.primed = True
            if tracker.sent is not None:   # 첫 폴링 이후에 명단이 도착 → 지금까지의 상황은 알리지 않는다
                tracker.sent |= {a["key"] for a in alerts}
        self.last = alerts
        return alerts
