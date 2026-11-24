"""리그 데스크 2: 전체 순위표 · 어젯밤 추천 경기 · 오늘 볼만한 경기 · 파워랭킹 · 닉스 다음 3경기 · 기록 임박 · 라이벌 감시.

데이터
- 리그 전체 일정·결과: nba_api ScheduleLeagueV2 (league/schedule_<시즌>.json)
- 순위(홈/원정·최근 10경기 포함): nba_api LeagueStandingsV3
- 경기 내용(리드 체인지·최대 리드·연장·선수 기록): cdn.nba.com 박스스코어 / 문자중계
- 경기 전 배당: ESPN 경기 요약의 pickcenter (DraftKings 머니라인)
- 넷레이팅: nba_api LeagueDashTeamStats (Advanced, 시즌 전체 / 최근 10경기)
- 통산 기록: nba_api AllTimeLeadersGrids (역대 상위 1000명, 정규시즌)
- 부상: ESPN 리그 부상자 목록
모든 쓰기는 test_live/league/ 안에서만.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import briefing
import league as LG
import resilient as RS
import nba_http as NH
import rivals as RV
import tracker as T

ET = ZoneInfo("America/New_York")
RIVALS = ["BOS", "PHI", "CLE"]
COUNTED = {"002", "004", "005", "006"}          # 정규시즌·플레이오프·플레이인·컵 결승 (프리시즌 제외)
TEAM_EN: dict[str, str] = {}


def now_kst() -> datetime:
    """시험용: LEAGUE_NOW=2026-03-16T09:00 처럼 넣으면 그 시각(한국 기준)으로 계산한다."""
    v = os.environ.get("LEAGUE_NOW")
    return datetime.fromisoformat(v).replace(tzinfo=T.KST) if v else datetime.now(T.KST)


def slate_days(now: datetime | None = None) -> tuple[date, date]:
    """(오늘 볼 경기의 미국 날짜, 어젯밤 경기의 미국 날짜).
    한국 오전 경기 = 미국 전날 저녁 경기라서, 한국 시각에서 13시간을 뺀 날짜가 '오늘 경기'의 미국 날짜다."""
    g = ((now or now_kst()) - timedelta(hours=13)).date()
    return g, g - timedelta(days=1)


def _kst(utc: str) -> datetime:
    return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(T.KST)


def _kst_label(utc: str) -> str:
    d = _kst(utc)
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]}) {d:%H:%M}"


def tko(tri: str) -> str:
    return LG.TEAM_KO.get(tri, tri)


# ---------------------------------------------------------------- 리그 전체 일정

def schedule(s: str) -> list[dict]:
    def fetch():
        from nba_api.stats.endpoints import scheduleleaguev2
        df = RV._stats_call(lambda: scheduleleaguev2.ScheduleLeagueV2(season=s, timeout=30).get_data_frames()[0])
        out = []
        for r in df.itertuples():
            if r.gameId[:3] not in COUNTED or not r.homeTeam_teamTricode:
                continue
            tv = getattr(r, "nationalBroadcasters_0_broadcasterDisplay", None)
            out.append({"gid": r.gameId, "utc": r.gameDateTimeUTC, "et_date": datetime.fromisoformat(r.gameDateTimeUTC.replace("Z", "+00:00")).astimezone(ET).date().isoformat(),
                        "home": r.homeTeam_teamTricode, "away": r.awayTeam_teamTricode, "status": int(r.gameStatus),
                        "hs": int(r.homeTeam_score or 0), "as": int(r.awayTeam_score or 0), "type": r.gameId[:3],
                        "home_en": f"{r.homeTeam_teamCity} {r.homeTeam_teamName}", "away_en": f"{r.awayTeam_teamCity} {r.awayTeam_teamName}",
                        "tv": tv if isinstance(tv, str) and tv else None, "label": str(r.gameLabel or "") if str(r.gameLabel or "") != "nan" else ""})
        return sorted(out, key=lambda g: g["utc"])
    games = LG._cached(f"schedule_{s}.json", fetch, timedelta(hours=2))
    for g in games:
        TEAM_EN[g["home"]], TEAM_EN[g["away"]] = g["home_en"], g["away_en"]
    return games


def _final(g: dict) -> bool:
    return g["status"] == 3


def _result(g: dict, team: str) -> dict:
    home = g["home"] == team
    my, op = (g["hs"], g["as"]) if home else (g["as"], g["hs"])
    return {"date": g["et_date"], "opp": g["away"] if home else g["home"], "home": home, "my": my, "op": op,
            "wl": "W" if my > op else "L", "gid": g["gid"]}


def team_games(s: str, team: str) -> list[dict]:
    return [g for g in schedule(s) if team in (g["home"], g["away"])]


# ---------------------------------------------------------------- 1) 전체 순위표

def standings_full(s: str) -> dict:
    def rows_of(ss):
        def fetch():
            from nba_api.stats.endpoints import leaguestandingsv3
            df = RV._stats_call(lambda: leaguestandingsv3.LeagueStandingsV3(season=ss, timeout=30).get_data_frames()[0])
            return [{"team": LG._team_tri(r.TeamID), "conf": r.Conference, "rank": int(r.PlayoffRank),
                     "w": int(r.WINS), "l": int(r.LOSSES), "pct": float(r.WinPCT), "gb": r.ConferenceGamesBack,
                     "streak": r.strCurrentStreak, "streak_n": int(r.CurrentStreak), "l10": r.L10, "home": r.HOME, "road": r.ROAD}
                    for r in df.itertuples()]
        return fetch
    rows = LG._cached(f"standings_full_{s}.json", rows_of(s), timedelta(hours=1))
    live = any(r["w"] + r["l"] for r in rows)
    label = f"{s} 정규시즌"
    if not live:
        prev = RV._prev(s)
        rows = LG._cached(f"standings_full_{prev}.json", rows_of(prev), None)
        label = f"{prev} 최종 (새 시즌 개막 전)"
    # 지난주 대비: 주마다 그 주의 마지막 순위를 남기고, 바로 전 주의 마지막 순위와 비교
    wk_path = LG._path(f"standings_weekly_{s}.json")
    weekly = RV._read(wk_path, {})
    week = LG._iso_week(now_kst())
    prev_week = None
    if live:
        weekly[week] = {r["team"]: r["rank"] for r in rows}
        RV._write(wk_path, weekly)
        older = sorted(w for w in weekly if w < week)
        prev_week = older[-1] if older else None
    base = weekly.get(prev_week, {}) if prev_week else {}
    # 리그 전체 순위(승률 순, 같으면 승수) — 파워랭킹과 비교용
    for i, r in enumerate(sorted(rows, key=lambda r: (-r["pct"], -r["w"])), 1):
        r["lrank"] = i
    out = {"label": label, "live": live, "week": week, "prev_week": prev_week,
           "source": {"name": "NBA.com 순위", "url": "https://www.nba.com/standings"}}
    for conf in ("East", "West"):
        cr = sorted([dict(r) for r in rows if r["conf"] == conf], key=lambda r: r["rank"])
        for r in cr:
            r["prev_rank"] = base.get(r["team"])
            r["tier"] = 1 if r["rank"] <= 6 else 2 if r["rank"] <= 10 else 3
        out[conf] = cr
    # 닉스 바로 위·아래 팀과 승차
    for conf in ("East", "West"):
        cr = out[conf]
        i = next((k for k, r in enumerate(cr) if r["team"] == "NYK"), None)
        if i is None:
            continue
        me = cr[i]
        gap = lambda o: ((o["w"] - me["w"]) + (me["l"] - o["l"])) / 2
        out["nyk"] = {"conf": conf, "rank": me["rank"],
                      "above": {"team": cr[i - 1]["team"], "rank": cr[i - 1]["rank"], "gap": gap(cr[i - 1])} if i > 0 else None,
                      "below": {"team": cr[i + 1]["team"], "rank": cr[i + 1]["rank"], "gap": -gap(cr[i + 1])} if i + 1 < len(cr) else None}
    return out


# ---------------------------------------------------------------- 넷레이팅 (시즌 / 최근 10경기)

def team_ratings(s: str, last_n: int = 0) -> dict[str, dict]:
    def fetch():
        from nba_api.stats.endpoints import leaguedashteamstats
        df = RV._stats_call(lambda: leaguedashteamstats.LeagueDashTeamStats(
            season=s, measure_type_detailed_defense="Advanced", per_mode_detailed="PerGame", last_n_games=last_n, timeout=30).get_data_frames()[0])
        return {LG._team_tri(r.TEAM_ID): {"net": float(r.NET_RATING), "off": float(r.OFF_RATING), "def": float(r.DEF_RATING),
                                          "gp": int(r.GP), "w": int(r.W), "l": int(r.L)} for r in df.itertuples()}
    try:
        return LG._cached(f"ratings_{s}_l{last_n}.json", fetch, timedelta(hours=3))
    except Exception:
        return {}


def strength(s: str) -> tuple[dict[str, float], str]:
    """매치업 수준용 팀 넷레이팅. 이번 시즌 5경기 미만이면 지난 시즌 값 (화면에 그렇게 적는다)."""
    cur = team_ratings(s)
    if cur and min(v["gp"] for v in cur.values()) >= 5:
        return {k: v["net"] for k, v in cur.items()}, f"{s} 넷레이팅"
    prev = RV._prev(s)
    return {k: v["net"] for k, v in team_ratings(prev).items()}, f"{prev} 넷레이팅 (이번 시즌 5경기 전)"


# ---------------------------------------------------------------- 4) 파워랭킹 (주 1회)

POWER_W = (0.65, 0.35)   # 시즌 넷레이팅, 최근 10경기 넷레이팅


def build_power(s: str) -> dict | None:
    season_r, l10 = team_ratings(s), team_ratings(s, 10)
    if not season_r or min(v["gp"] for v in season_r.values()) < 3:
        return None
    st = standings_full(s)
    actual = {r["team"]: r["lrank"] for c in ("East", "West") for r in st[c]}
    rows = []
    for t, v in season_r.items():
        f = l10.get(t, v)
        rows.append({"team": t, "net": round(v["net"], 1), "l10_net": round(f["net"], 1), "l10": f"{f['w']}-{f['l']}",
                     "w": v["w"], "l": v["l"], "score": round(POWER_W[0] * v["net"] + POWER_W[1] * f["net"], 2), "actual": actual.get(t)})
    rows.sort(key=lambda r: -r["score"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["gap"] = (r["actual"] - i) if r["actual"] else None   # +면 실제 순위보다 파워랭킹이 높다(저평가)
    return {"rows": rows, "built_kst": now_kst().isoformat(timespec="seconds")}


def power(s: str) -> dict:
    path = LG._path(f"power_{s}.json")
    hist = RV._read(path, {})
    week = LG._iso_week(now_kst())
    if week not in hist:
        p = build_power(s)
        if p:
            hist[week] = p
            RV._write(path, hist)
    weeks = sorted(hist)
    if not weeks:
        return {"rows": [], "week": week, "note": "이번 시즌 팀마다 3경기 이상 치른 뒤 첫 월요일에 첫 순위를 냅니다."}
    cur = hist[weeks[-1]]
    prev = {r["team"]: r["rank"] for r in hist[weeks[-2]]["rows"]} if len(weeks) > 1 else {}
    rows = [{**r, "prev_rank": prev.get(r["team"])} for r in cur["rows"]]
    return {"rows": rows, "week": weeks[-1], "prev_week": weeks[-2] if len(weeks) > 1 else None, "built_kst": cur["built_kst"],
            "rule": f"점수 = 시즌 넷레이팅 × {POWER_W[0]} + 최근 10경기 넷레이팅 × {POWER_W[1]} · 매주 처음 계산한 값으로 고정 · "
                    "실제 순위(리그 전체 승률 순)와 5계단 이상 차이 나면 표시",
            "source": {"name": "NBA.com 팀 고급 기록", "url": "https://www.nba.com/stats/teams/advanced"}}


# ---------------------------------------------------------------- 부상 (ESPN 리그 전체)

DETAIL_EXTRA = {"Ruptured": "파열", "Rupture": "파열", "Tear": "파열", "Torn": "파열", "Fracture": "골절", "Surgery": "수술",
                "Soreness": "통증", "Tightness": "뭉침", "Inflammation": "염증", "Tendinitis": "건염", "Dislocation": "탈구",
                "Spasms": "경련", "Impingement": "충돌 증후군", "Stress Reaction": "피로 골절 전 단계", "Not Specified": ""}
REST = {"휴식", "감독 결정", "개인 사유", "팀 미합류"}


def injuries_all() -> dict[str, list[dict]]:
    def fetch():
        j = briefing._get(f"{briefing.ESPN}/injuries")
        out = {}
        for team in j.get("injuries", []):
            tri = LG.ESPN_TO_TRI.get(int(team["id"])) if str(team.get("id", "")).isdigit() else None
            if not tri:
                continue
            rows = []
            for i in team.get("injuries", []):
                d = i.get("details", {}) or {}
                status = (i.get("status") or "").strip()
                typ = d.get("type") or ""
                det = d.get("detail") or ""
                part = briefing.REASON_KO.get(typ) or " ".join(x for x in (briefing.SIDE_KO.get(d.get("side"), ""), briefing.BODY_KO.get(typ, typ),
                                                                         briefing.DETAIL_KO.get(det) or DETAIL_EXTRA.get(det, det)) if x)
                nm = i["athlete"].get("displayName")
                rows.append({"name": nm, "name_ko": LG.ko_name(nm), "status": briefing.STATUS_KO.get(status.lower(), status), "status_raw": status,
                             "part": part, "return_date": d.get("returnDate"), "updated": i.get("date"),
                             "link": f"https://www.espn.com/nba/team/injuries/_/name/{tri.lower()}"})
            out[tri] = rows
        return out
    return LG._cached("injuries_espn.json", fetch, timedelta(minutes=30))


def _inj_sorted(rows: list[dict], star: dict) -> list[dict]:
    order = {"out": 0, "doubtful": 1, "questionable": 2, "day-to-day": 3}
    return sorted(rows, key=lambda r: (order.get(r["status_raw"].lower(), 4), r["name"] not in star))


# ---------------------------------------------------------------- 2) 어젯밤 재미있었던 경기

def _clock_sec(c: str) -> float:
    # "PT01M23.40S"
    try:
        m = c.split("PT")[1].split("M")
        return int(m[0]) * 60 + float(m[1].rstrip("S"))
    except Exception:
        return 0.0


def _espn_odds(day: date) -> dict[tuple[str, str], dict]:
    """ESPN 그날 경기의 경기 전 머니라인 → {(원정, 홈): {...}}"""
    out = {}
    sb = briefing._get(f"{briefing.ESPN}/scoreboard", dates=day.strftime("%Y%m%d"))
    for ev in sb.get("events", []):
        comp = ev["competitions"][0]
        side = {c["homeAway"]: briefing.ESPN_TO_NBA.get(c["team"]["abbreviation"], c["team"]["abbreviation"]) for c in comp["competitors"]}
        try:
            pc = (briefing._get(f"{briefing.ESPN}/summary", event=ev["id"]).get("pickcenter") or [None])[0]
        except Exception:
            pc = None
        if not pc:
            continue
        out[(side.get("away"), side.get("home"))] = {
            "provider": pc.get("provider", {}).get("name"), "details": pc.get("details"),
            "home_ml": (pc.get("homeTeamOdds") or {}).get("moneyLine"), "away_ml": (pc.get("awayTeamOdds") or {}).get("moneyLine"),
            "url": f"https://www.espn.com/nba/game/_/gameId/{ev['id']}"}
    return out


def _game_facts(g: dict) -> dict:
    box = T.cdn_get(f"boxscore/boxscore_{g['gid']}.json")["game"]
    h, a = box["homeTeam"], box["awayTeam"]
    hs, as_ = int(h["score"]), int(a["score"])
    win, lose = (h, a) if hs > as_ else (a, h)
    players = []
    for team in (h, a):
        for p in team["players"]:
            st = p.get("statistics", {})
            if not st.get("minutes") or st.get("minutes") == "PT00M00.00S":
                continue
            players.append({"id": p["personId"], "name": p.get("name") or f"{p.get('firstName', '')} {p.get('familyName', '')}".strip(), "team": team["teamTricode"],
                            "pts": st.get("points", 0), "reb": st.get("reboundsTotal", 0), "ast": st.get("assists", 0),
                            "stl": st.get("steals", 0), "blk": st.get("blocks", 0)})
    acts = T.cdn_get(f"playbyplay/playbyplay_{g['gid']}.json")["game"]["actions"]
    margin_2min, last_flip, last_lead = None, None, 0
    reg_end = 4
    for x in acts:
        if x.get("scoreHome") in (None, ""):
            continue
        d = int(x["scoreHome"]) - int(x["scoreAway"])
        sec = _clock_sec(x.get("clock", ""))
        if x["period"] == reg_end and sec <= 120 and margin_2min is None:
            margin_2min = abs(d)
        lead = (d > 0) - (d < 0)
        if lead and lead != last_lead:
            if last_lead:
                last_flip = {"period": x["period"], "sec": sec}
            last_lead = lead
    return {"home": h["teamTricode"], "away": a["teamTricode"], "hs": hs, "as": as_, "winner": win["teamTricode"],
            "ot": max(0, int(box.get("period", 4)) - 4), "lead_changes": int(h["statistics"].get("leadChanges", 0)),
            "ties": int(h["statistics"].get("timesTied", 0)), "comeback": int(lose["statistics"].get("biggestLead", 0)),
            "margin_2min": margin_2min, "final_margin": abs(hs - as_), "players": players,
            "late_flip": bool(last_flip and last_flip["period"] >= 4 and last_flip["sec"] <= 5 and last_flip["period"] == int(box.get("period", 4)))}


def _villain_ids() -> dict[int, str]:
    out = {}
    for pid, n in RV.rivals_notes().items():
        if n.get("memo") or n.get("tags"):
            out[int(pid)] = LG.ko_name(n.get("name") or str(pid))
    return out


def rate_game(f: dict, odds: dict | None, roy_ids: dict[int, str], villains: dict[int, str]) -> dict:
    score, tags, spoil = 0.0, [], []
    fm = f["final_margin"]
    if f["ot"]:
        score += min(5, 3 * f["ot"]); tags.append(f"{f['ot']}차 연장" if f["ot"] > 1 else "연장전")
    if fm <= 3:
        score += 3
        if not f["ot"]:
            tags.append("마지막까지 접전")
    elif fm <= 6:
        score += 1.5
    if f["margin_2min"] is not None and f["margin_2min"] <= 5 and not f["ot"]:
        score += 2
        if "마지막까지 접전" not in tags:
            tags.append("종료 2분 전 5점 차 이내")
    if f["late_flip"]:
        score += 3; tags.append("마지막 5초 역전")
    lc = f["lead_changes"]
    score += min(3, lc / 5)
    if lc >= 10:
        tags.append(f"리드 체인지 {lc}번")
    cb = f["comeback"]
    if cb >= 10:
        score += 4 if cb >= 20 else 3 if cb >= 15 else 2
        tags.append(f"{cb}점 차 역전승")
    big = [p for p in f["players"] if p["pts"] >= 40]
    tds = [p for p in f["players"] if sum(p[k] >= 10 for k in ("pts", "reb", "ast", "stl", "blk")) >= 3]
    for p in big:
        score += 4 if p["pts"] >= 50 else 2
    score += 2 * len(tds)
    if big:
        tags.append("50점 경기 나옴" if any(p["pts"] >= 50 for p in big) else "40점 이상 선수 나옴")
    if tds:
        tags.append("트리플더블 나옴")
    upset = None
    if odds and odds.get("home_ml") is not None and odds.get("away_ml") is not None:
        w_ml = odds["home_ml"] if f["winner"] == f["home"] else odds["away_ml"]
        if w_ml >= 150:
            score += 3 if w_ml >= 250 else 2
            upset = w_ml
            spoil.append(f"배당 기준 이변 (승리 팀 경기 전 +{int(w_ml)})")
    on_court = {p["id"] for p in f["players"]}
    roy = [nm for pid, nm in roy_ids.items() if pid in on_court]
    if roy:
        score += len(roy); tags.append("신인왕 후보 " + ", ".join(roy))
    riv = [t for t in (f["home"], f["away"]) if t in RIVALS]
    if riv:
        score += 1; tags.append("닉스 라이벌 " + "·".join(riv))
    vil = [nm for pid, nm in villains.items() if pid in on_court]
    if vil:
        score += len(vil); tags.append("내 빌런 메모 " + ", ".join(vil))
    stars = 1 if score < 3 else 2 if score < 6 else 3 if score < 9 else 4 if score < 12 else 5
    return {"score": round(score, 1), "stars": stars, "tags": [t for t in tags if t], "spoiler_tags": spoil, "upset_ml": upset}


def build_recap(s: str, day: date, roy_ids: dict[int, str]) -> dict:
    games = [g for g in schedule(s) if g["et_date"] == day.isoformat()]
    out = {"et_date": day.isoformat(), "games": [], "errors": [], "built_kst": now_kst().isoformat(timespec="seconds")}
    if not games:
        return out
    try:
        odds = _espn_odds(day)
    except Exception as exc:
        odds = {}
        out["errors"].append(RS.friendly("배당", exc))
    villains = _villain_ids()
    done = 0
    for g in games:
        item = {"gid": g["gid"], "home": g["home"], "away": g["away"], "utc": g["utc"], "kst": _kst_label(g["utc"]),
                "type": g["type"], "label": g["label"]}
        try:
            f = _game_facts(g)
        except Exception as exc:
            out["errors"].append(RS.friendly(f"{g['away']}@{g['home']} 경기 기록", exc, g["gid"]))
            continue
        if f["hs"] == f["as"]:
            item["pending"] = True
            out["games"].append(item)
            continue
        box_status = T.cdn_get(f"boxscore/boxscore_{g['gid']}.json")["game"].get("gameStatus")
        if box_status != 3:
            item["pending"] = True
            out["games"].append(item)
            continue
        done += 1
        o = odds.get((g["away"], g["home"]))
        r = rate_game(f, o, roy_ids, villains)
        star_lines = [f"{LG.ko_name(p['name'])} {p['pts']}점 {p['reb']}리바 {p['ast']}어시" for p in f["players"]
                      if p["pts"] >= 40 or sum(p[k] >= 10 for k in ("pts", "reb", "ast", "stl", "blk")) >= 3]
        item.update(r, result={"hs": f["hs"], "as": f["as"], "winner": f["winner"], "ot": f["ot"], "lines": star_lines},
                    facts={k: f[k] for k in ("lead_changes", "ties", "comeback", "margin_2min", "ot")}, odds=o,
                    box=f"https://www.nba.com/game/{g['gid']}/box-score")
        out["games"].append(item)
        time.sleep(0.2)
    out["complete"] = done == len(games)
    rated = sorted([x for x in out["games"] if "score" in x], key=lambda x: -x["score"])
    for i, x in enumerate(rated):
        x["order"] = i + 1
        if i < 3:
            when = datetime.fromisoformat(day.isoformat())
            q = f"{TEAM_EN.get(x['away'], x['away'])} vs {TEAM_EN.get(x['home'], x['home'])} highlights {when:%B} {when.day}, {when.year}"
            x["yt"] = "https://www.youtube.com/@NBA/search?query=" + quote(q)
    out["games"].sort(key=lambda x: (x.get("order") or 99, x["utc"]))
    return out


# ---------------------------------------------------------------- 3) 오늘 볼만한 경기

def _prev_playoff_pairs(s: str) -> set[frozenset]:
    try:
        return {frozenset((g["home"], g["away"])) for g in schedule(RV._prev(s)) if g["type"] == "004"}
    except Exception:
        return set()


def build_guide(s: str, day: date, roy: list[dict], next_opp: str | None) -> dict:
    sched = schedule(s)
    look, label = day, "오늘"
    games = [g for g in sched if g["et_date"] == look.isoformat()]
    if not games:   # 오늘 경기가 없으면 다음 경기일
        later = sorted({g["et_date"] for g in sched if g["et_date"] > day.isoformat()})
        if later:
            look, label = date.fromisoformat(later[0]), "다음 경기일"
            games = [g for g in sched if g["et_date"] == later[0]]
    games = [g for g in games if "NYK" not in (g["home"], g["away"])]
    net, net_label = strength(s)
    st = standings_full(s)
    rank = {r["team"]: r for c in ("East", "West") for r in st[c]} if st["live"] else {}
    exk = LG.ex_knicks(s)
    pairs = _prev_playoff_pairs(s)
    roy_team: dict[str, list[str]] = {}
    for c in roy:
        roy_team.setdefault(c["team"], []).append(c["name_ko"])
    rated = []
    for g in games:
        h, a = g["home"], g["away"]
        why, score = [], 0.0
        nh, na = net.get(h), net.get(a)
        if nh is not None and na is not None:
            lvl = (nh + na) / 2
            score += max(0, lvl + 4) * 0.8          # 두 팀 평균 넷레이팅 -4 이상부터 점수
            score += max(0, 6 - abs(nh - na)) * 0.4  # 전력이 비슷할수록
            if nh >= 3 and na >= 3:
                why.append(f"강팀 맞대결 (넷레이팅 {nh:+.1f} vs {na:+.1f})")
            elif abs(nh - na) <= 2 and lvl >= 0:
                why.append(f"전력이 비슷한 두 팀 (넷레이팅 {nh:+.1f} vs {na:+.1f})")
        rh, ra = rank.get(h), rank.get(a)
        if rh and ra and rh["conf"] == ra["conf"]:
            gb = abs(((rh["w"] - ra["w"]) + (ra["l"] - rh["l"])) / 2)
            lo, hi = sorted((rh["rank"], ra["rank"]))
            if gb <= 2 and hi <= 12:
                zone = "1위 경쟁" if lo == 1 else "직행(6위) 경계" if lo <= 6 < hi or hi in (6, 7) else "플레이인(10위) 경계" if lo <= 10 < hi or hi in (10, 11) else "순위 경쟁"
                score += 4 - gb
                why.append(f"{'동부' if rh['conf'] == 'East' else '서부'} {lo}위·{hi}위 {zone} (승차 {gb:g})")
        if frozenset((h, a)) in pairs:
            score += 2; why.append("지난 시즌 플레이오프 리매치")
        rc = roy_team.get(h, []) + roy_team.get(a, [])
        if len(rc) >= 2 and roy_team.get(h) and roy_team.get(a):
            score += 4; why.append("신인왕 후보 맞대결: " + " vs ".join(rc))
        elif rc:
            score += 1.5; why.append("신인왕 후보 " + ", ".join(rc))
        if next_opp in (h, a):
            score += 2; why.append(f"닉스 다음 상대 {next_opp} 미리 보기")
        riv = [t for t in (h, a) if t in RIVALS]
        if riv:
            score += 1; why.append("닉스 라이벌 " + "·".join(riv))
        try:
            exs = [p["name"] for t in (h, a) for p in RV._roster(t, s) if p["id"] in exk]
        except Exception:
            exs = []
        if exs:
            score += 1; why.append("전 닉스 " + ", ".join(LG.ko_name(n) for n in exs[:3]))
        if g["tv"]:
            score += 0.5
        rated.append({"gid": g["gid"], "home": h, "away": a, "kst": _kst_label(g["utc"]), "utc": g["utc"], "score": round(score, 1),
                      "why": why, "tv": g["tv"], "net": [na, nh], "rank": [ra and ra["rank"], rh and rh["rank"]]})
    rated.sort(key=lambda x: -x["score"])
    pick = rated[:1] + [x for x in rated[1:2] if x["score"] >= 0.6 * rated[0]["score"] and x["why"]]
    return {"label": label, "et_date": look.isoformat(), "kst_date": _kst(pick[0]["utc"]).date().isoformat() if pick else None,
            "picks": pick, "others": [{k: x[k] for k in ("home", "away", "kst", "score")} for x in rated[len(pick):]],
            "count": len(games), "net_label": net_label, "standings_live": st["live"],
            "rule": "매치업 수준(두 팀 넷레이팅 평균·전력 차) + 순위 경쟁(같은 콘퍼런스 승차 2경기 이내) + 스토리(신인왕 후보·플레이오프 리매치·닉스 다음 상대·라이벌·전 닉스) · 닉스 경기는 제외"}


# ---------------------------------------------------------------- 5) 닉스 다음 3경기 레이더

def build_radar(s: str) -> dict:
    sched = schedule(s)
    now = datetime.now(timezone.utc) if not os.environ.get("LEAGUE_NOW") else now_kst().astimezone(timezone.utc)
    at = lambda g: datetime.fromisoformat(g["utc"].replace("Z", "+00:00"))
    upcoming = [g for g in sched if "NYK" in (g["home"], g["away"]) and at(g) > now - timedelta(hours=3)
                and not (_final(g) and at(g) < now)][:3]
    inj = {}
    try:
        inj = injuries_all() if not os.environ.get("LEAGUE_NOW") else {}
    except Exception:
        pass
    star = LG.stars(s)
    prev_sched = None
    out = []
    for g in upcoming:
        opp = g["away"] if g["home"] == "NYK" else g["home"]
        played = [x for x in team_games(s, opp) if _final(x) and x["utc"] < g["utc"]]
        basis = f"{s} 시즌"
        if not played:
            prev_sched = prev_sched or schedule(RV._prev(s))
            played = [x for x in prev_sched if opp in (x["home"], x["away"]) and _final(x) and x["type"] == "002"]
            basis = f"{RV._prev(s)} 정규시즌 마지막 5경기 (이번 시즌 경기 전)"
        last5 = [_result(x, opp) for x in played[-5:]]
        gday = date.fromisoformat(g["et_date"])
        yday = (gday - timedelta(days=1)).isoformat()
        b2b = lambda t: next((x for x in team_games(s, t) if x["et_date"] == yday), None)
        ob, nb = b2b(opp), b2b("NYK")
        rows = _inj_sorted(inj.get(opp, []), star)
        out.append({"gid": g["gid"], "opp": opp, "home": g["home"] == "NYK", "kst": _kst_label(g["utc"]), "utc": g["utc"],
                    "last5": last5, "last5_basis": basis, "w5": sum(r["wl"] == "W" for r in last5),
                    "b2b_opp": ({"vs": ob["away"] if ob["home"] == opp else ob["home"], "home": ob["home"] == opp} if ob else None),
                    "b2b_nyk": ({"vs": nb["away"] if nb["home"] == "NYK" else nb["home"]} if nb else None),
                    "injuries": rows[:6], "inj_more": max(0, len(rows) - 6),
                    "inj_link": f"https://www.espn.com/nba/team/injuries/_/name/{opp.lower()}"})
    return {"games": out, "built_kst": now_kst().isoformat(timespec="seconds"),
            "source": "일정·결과 NBA.com · 부상 ESPN 부상자 목록 · 백투백 = 전날(미국 날짜)에도 경기"}


# ---------------------------------------------------------------- 6) 기록 임박

MILESTONES = {
    "PTS": ("득점", [10000, 15000, 20000, 25000, 30000, 35000, 40000, 45000]),
    "REB": ("리바운드", [5000, 7500, 10000, 12500, 15000]),
    "AST": ("어시스트", [3000, 5000, 7500, 10000, 12500]),
    "STL": ("스틸", [1000, 1500, 2000, 2500]),
    "BLK": ("블록", [1000, 1500, 2000, 2500, 3000]),
    "FG3M": ("3점슛", [1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500]),
    "GP": ("출전 경기", [1000, 1250, 1500]),
}
PASS_TOP = 25   # 역대 25위 안에서 한 계단 오르는 것만 '이정표'로 본다
WITHIN = 10


def career_leaders() -> dict[str, list[dict]]:
    def fetch():
        from nba_api.stats.endpoints import alltimeleadersgrids
        dfs = RV._stats_call(lambda: alltimeleadersgrids.AllTimeLeadersGrids(topx=1000, timeout=60).get_data_frames())
        out = {}
        for df in dfs:
            k = df.columns[2]
            if k in MILESTONES:
                out[k] = [{"id": int(r.PLAYER_ID), "name": r.PLAYER_NAME, "v": int(getattr(r, k)), "rank": int(getattr(r, f"{k}_RANK")),
                           "active": r.IS_ACTIVE_FLAG == "Y"} for r in df.itertuples()]
        return out
    return LG._cached("career_leaders.json", fetch, timedelta(hours=12))


def per_game(s: str) -> tuple[dict[int, dict], str]:
    def fetch(ss):
        def f():
            from nba_api.stats.endpoints import leaguedashplayerstats
            df = RV._stats_call(lambda: leaguedashplayerstats.LeagueDashPlayerStats(season=ss, per_mode_detailed="PerGame", timeout=30).get_data_frames()[0])
            return {str(int(r.PLAYER_ID)): {"team": r.TEAM_ABBREVIATION, "gp": int(r.GP), "PTS": r.PTS, "REB": r.REB, "AST": r.AST, "STL": r.STL,
                                            "BLK": r.BLK, "FG3M": r.FG3M} for r in df.itertuples()}
        return f
    cur = {int(k): v for k, v in LG._cached(f"pergame_{s}.json", fetch(s), timedelta(hours=6)).items()}
    prev = {int(k): v for k, v in LG._cached(f"pergame_{RV._prev(s)}.json", fetch(RV._prev(s)), None).items()}
    out = {}
    for pid in set(cur) | set(prev):
        c, p = cur.get(pid), prev.get(pid)
        out[pid] = {**c, "basis": "이번 시즌"} if c and c["gp"] >= 5 else ({**p, "basis": "지난 시즌"} if p else {**c, "basis": "이번 시즌"})
        if c:
            out[pid]["team"] = c["team"]
    return out, f"{s} 경기당 기록 (5경기 미만이면 {RV._prev(s)})"


def build_records(s: str) -> dict:
    lead = career_leaders()
    pace, pace_label = per_game(s)
    current = {p["id"]: p["team"] for p in LG.all_players(s) if p.get("team")}
    items = []
    for k, (ko, marks) in MILESTONES.items():
        rows = lead.get(k, [])
        for i, r in enumerate(rows):
            if not r["active"] or r["id"] not in current:
                continue
            pg = 1.0 if k == "GP" else (pace.get(r["id"]) or {}).get(k) or 0
            if pg <= 0:
                continue
            team = current[r["id"]]
            nxt = next((m for m in marks if m > r["v"]), None)
            if nxt:
                need = nxt - r["v"]
                g = math.ceil(need / pg)
                if g <= WITHIN:
                    items.append({"kind": "mark", "cat": k, "cat_ko": ko, "id": r["id"], "name": r["name"], "name_ko": LG.ko_name(r["name"]),
                                  "team": team, "now": r["v"], "target": nxt, "need": need, "games": g, "pg": round(pg, 1),
                                  "text": f"통산 {ko} {nxt:,}개" if k not in ("PTS", "GP") else (f"통산 {nxt:,}점" if k == "PTS" else f"통산 {nxt:,}경기 출전"),
                                  "rank": r["rank"], "basis": (pace.get(r["id"]) or {}).get("basis", "")})
            if 1 < r["rank"] <= PASS_TOP and i > 0:
                above = rows[i - 1]
                need = above["v"] - r["v"] + 1
                g = math.ceil(need / pg)
                if g <= WITHIN and above["rank"] < r["rank"]:
                    items.append({"kind": "pass", "cat": k, "cat_ko": ko, "id": r["id"], "name": r["name"], "name_ko": LG.ko_name(r["name"]),
                                  "team": team, "now": r["v"], "target": above["v"] + 1, "need": need, "games": g, "pg": round(pg, 1),
                                  "text": f"역대 {ko} {r['rank']}위 → {above['rank']}위 ({LG.ko_name(above['name'])} {above['v']:,} 넘기)",
                                  "rank": r["rank"], "basis": (pace.get(r["id"]) or {}).get("basis", "")})
    items.sort(key=lambda x: (x["games"], x["rank"]))
    return {"items": items[:20], "total": len(items), "pace_label": pace_label, "built_kst": now_kst().isoformat(timespec="seconds"),
            "rule": f"정규시즌 통산 기록 기준. 남은 수 ÷ 경기당 기록 = 필요한 경기 수, {WITHIN}경기 이내만. "
                    f"이정표 = 득점 5천 단위·리바운드/어시스트·3점슛 등 큰 단위 숫자, 또는 역대 {PASS_TOP}위 안에서 한 계단 오르기. 매 경기 뛴다고 가정",
            "source": {"name": "NBA.com 역대 기록 (AllTimeLeadersGrids)", "url": "https://www.nba.com/stats/alltime-leaders"}}


# ---------------------------------------------------------------- 7) 라이벌 감시

def build_rivals(s: str, day: date) -> dict:
    sched = schedule(s)
    try:
        inj = injuries_all() if not os.environ.get("LEAGUE_NOW") else {}
    except Exception:
        inj = {}
    star = LG.stars(s)
    out = []
    for t in RIVALS:
        mine = [g for g in sched if t in (g["home"], g["away"]) and _final(g) and g["et_date"] <= day.isoformat()]
        y = next((g for g in mine if g["et_date"] == day.isoformat()), None)
        last = y or (mine[-1] if mine else None)
        res = _result(last, t) if last else None
        rows = [r for r in _inj_sorted(inj.get(t, []), star)
                if r["status_raw"].lower() in ("out", "doubtful", "questionable", "day-to-day") and r["part"] not in REST]
        # 한 줄에 올릴 선수: 결장·불투명 스타 → 결장 선수 → 나머지 순
        key = [r for r in rows if r["name"] in star and r["status_raw"].lower() in ("out", "doubtful")] or \
              [r for r in rows if r["status_raw"].lower() == "out"] or rows
        inj_line = None
        if key:
            k = key[0]
            part = f"({k['part']})" if k["part"] else ""
            inj_line = f"{k['name_ko']} {k['status']}{part}" + (f" 외 {len(rows) - 1}명" if len(rows) > 1 else "")
        out.append({"team": t, "played_yday": bool(y), "result": res, "inj": inj_line, "inj_n": len(rows),
                    "inj_link": f"https://www.espn.com/nba/team/injuries/_/name/{t.lower()}",
                    "box": f"https://www.nba.com/game/{res['gid']}/box-score" if res else None})
    return {"teams": out, "et_date": day.isoformat()}


# ---------------------------------------------------------------- 서비스

DESK_LABEL = {"standings": "순위", "recap": "어젯밤 경기", "guide": "오늘 경기 일정", "radar": "다음 3경기 정보",
              "rivals": "라이벌 기록", "power": "파워랭킹 기록", "records": "통산 기록"}


class Desk:
    """리그 데스크의 추가 카드들 (리그 전 경기 데이터). 하루 한 번만 계산한다: 한국 시간 15시 이후 처음,
    닉스 경기 시간(팁오프 1시간 전 ~ 경기 끝)이 아닐 때. 결과는 league/desk_state.json 에 남겨 서버를 다시 켜도 다시 받지 않는다."""

    STATE = os.path.join(LG.DIR, "desk_state.json")

    def __init__(self, league_desk, next_opp_fn):
        self.lg = league_desk
        self.next_opp_fn = next_opp_fn
        self.data: dict = {}
        self.errors: dict[str, str] = {}
        self.at = 0.0
        self.day: str | None = None
        self.busy = False
        self.started = time.time()
        saved = RV._read(self.STATE, None)
        if saved:
            self.data, self.errors, self.at, self.day = saved["data"], saved.get("errors", {}), saved["at"], saved["day"]

    def _roy(self) -> list[dict]:
        b = self.lg.board or {}
        return b.get("cands", [])

    def _run(self):
        s = LG.season()
        guide_day, recap_day = slate_days()
        roy = self._roy()
        roy_ids = {c["id"]: c["name_ko"] for c in roy if c.get("id")}
        jobs = {
            "standings": lambda: standings_full(s),
            "recap": lambda: self._recap(s, recap_day, roy_ids),
            "guide": lambda: build_guide(s, guide_day, roy, self.next_opp_fn()),
            "radar": lambda: build_radar(s),
            "rivals": lambda: build_rivals(s, recap_day),
            "power": lambda: power(s),
            "records": lambda: build_records(s),
        }
        for name, fn in jobs.items():
            try:
                self.data[name] = fn()
                self.errors.pop(name, None)
            except Exception as exc:
                self.errors[name] = RS.friendly(DESK_LABEL.get(name, name), exc, then="30분 뒤 다시 시도")
                print(f"[리그2] {name} 실패 ({type(exc).__name__}) → logs/fetch_errors.log", flush=True)
        self.at = time.time()
        self.day = NH.desk_day()
        RV._write(self.STATE, {"data": self.data, "errors": self.errors, "at": self.at, "day": self.day})
        print(f"[리그2] 갱신: 어젯밤 {len((self.data.get('recap') or {}).get('games', []))}경기 · 오늘 추천 "
              f"{len((self.data.get('guide') or {}).get('picks', []))} · 기록 임박 {len((self.data.get('records') or {}).get('items', []))}", flush=True)

    def _recap(self, s: str, day: date, roy_ids: dict) -> dict:
        path = LG._path(f"recap_{s}_{day.isoformat()}.json")
        saved = RV._read(path, None)
        if saved and saved.get("complete"):
            return saved
        r = build_recap(s, day, roy_ids)
        if r["games"]:
            RV._write(path, r)
        return r

    def tick(self) -> None:
        if self.busy or (self.data and self.day == NH.desk_day()):
            return   # 오늘 이미 받았다
        if NH.knicks_busy():   # 닉스 경기 시간엔 리그 전 경기 데이터를 받지 않는다
            return
        if not self.lg.board and time.time() - self.started < 180:   # 신인왕 후보(가산점)가 준비될 때까지 잠깐 기다린다
            return
        self.busy = True

        def run():
            try:
                self._run()
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def snapshot(self) -> dict:
        return {"desk": self.data, "desk_errors": self.errors, "desk_loading": self.busy and not self.data,
                "desk_built_kst": datetime.fromtimestamp(self.at, T.KST).isoformat(timespec="seconds") if self.at else None}
