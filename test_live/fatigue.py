"""일정·체력 지표 (양 팀 비교): 휴식일 · 백투백 · 빡빡한 일정 · 이동 거리.

데이터: NBA 공식 리그 전체 일정 (stats.nba.com ScheduleLeagueV2, 프리시즌 포함) → league/schedule_full_<시즌>.json
- 날짜는 미국 동부(경기 현지 달력) 기준. 휴식일 = 두 경기 날짜 사이의 빈 날 수 (전날 경기 = 0일 = 백투백)
- 이동 거리: 직전 경기 경기장 → 이번 경기장, 경기장 좌표로 하버사인(지구 반지름 6371 km) 계산.
  경기 사이에 집에 들렀다 가는 이동은 일정에 없어 셈하지 않는다.
- 최근 7일: 이번 경기 전 7일 동안 치른 경기 수, 그 경기들과 이번 경기에 도착하기까지의 이동 거리 합
결과(승패·점수)는 쓰지 않는다 — 지난 경기 화면에서도 '당시 일정 조건' 으로만 보여 준다.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import league as LG
import resilient as RS
import rivals as RV

SOURCE = {"name": "NBA 공식 일정 (stats.nba.com)", "url": "https://www.nba.com/schedule"}
TYPES = {"001": "프리시즌", "002": "정규시즌", "004": "플레이오프", "005": "플레이인", "006": "NBA컵 결승"}

# 경기장 좌표 (위도, 경도). 일정 데이터의 arenaName 그대로
ARENAS = {
    "State Farm Arena": (33.7573, -84.3963), "TD Garden": (42.3662, -71.0621), "Barclays Center": (40.6826, -73.9754),
    "Spectrum Center": (35.2251, -80.8392), "United Center": (41.8807, -87.6742), "Rocket Arena": (41.4965, -81.6882),
    "Rocket Mortgage FieldHouse": (41.4965, -81.6882), "American Airlines Center": (32.7905, -96.8103),
    "Ball Arena": (39.7487, -105.0077), "Little Caesars Arena": (42.3410, -83.0550), "Chase Center": (37.7680, -122.3877),
    "Toyota Center": (29.7508, -95.3621), "Gainbridge Fieldhouse": (39.7640, -86.1555), "Intuit Dome": (33.9447, -118.3414),
    "Crypto.com Arena": (34.0430, -118.2673), "FedExForum": (35.1382, -90.0506), "Kaseya Center": (25.7814, -80.1870),
    "Fiserv Forum": (43.0451, -87.9172), "Target Center": (44.9795, -93.2761), "Smoothie King Center": (29.9490, -90.0821),
    "Madison Square Garden": (40.7505, -73.9934), "Paycom Center": (35.4634, -97.5151), "Kia Center": (28.5392, -81.3839),
    "Xfinity Mobile Arena": (39.9012, -75.1720), "Wells Fargo Center": (39.9012, -75.1720),
    "Mortgage Matchup Center": (33.4457, -112.0712), "Footprint Center": (33.4457, -112.0712), "PHX Arena": (33.4457, -112.0712),
    "Moda Center": (45.5316, -122.6668), "Golden 1 Center": (38.5802, -121.4997), "Frost Bank Center": (29.4270, -98.4375),
    "Scotiabank Arena": (43.6435, -79.3791), "Delta Center": (40.7683, -111.9011), "Capital One Arena": (38.8981, -77.0209),
    # 홈 밖 경기장 (프리시즌·해외 경기 등)
    "Venetian Arena": (22.1468, 113.5627), "CU Events Center": (40.0055, -105.2610), "Arena CDMX": (19.4920, -99.1720),
    "Hilton Coliseum": (42.0222, -93.6355), "Stan Sheriff Center": (21.2960, -157.8190), "T-Mobile Arena": (36.1029, -115.1784),
    "Co-op Live": (53.4846, -2.1999), "BOK Center": (36.1525, -95.9963), "Accor Arena": (48.8386, 2.3786),
    "Moody Center": (30.2820, -97.7320), "Rogers Arena": (49.2778, -123.1088), "Videotron Centre": (46.8297, -71.2490),
    "Etihad Arena": (24.4700, 54.6050), "Uber Arena": (52.5053, 13.4436), "The O2": (51.5030, 0.0032),
}
HOME = {"ATL": "State Farm Arena", "BOS": "TD Garden", "BKN": "Barclays Center", "CHA": "Spectrum Center", "CHI": "United Center",
        "CLE": "Rocket Arena", "DAL": "American Airlines Center", "DEN": "Ball Arena", "DET": "Little Caesars Arena",
        "GSW": "Chase Center", "HOU": "Toyota Center", "IND": "Gainbridge Fieldhouse", "LAC": "Intuit Dome", "LAL": "Crypto.com Arena",
        "MEM": "FedExForum", "MIA": "Kaseya Center", "MIL": "Fiserv Forum", "MIN": "Target Center", "NOP": "Smoothie King Center",
        "NYK": "Madison Square Garden", "OKC": "Paycom Center", "ORL": "Kia Center", "PHI": "Xfinity Mobile Arena",
        "PHX": "Mortgage Matchup Center", "POR": "Moda Center", "SAC": "Golden 1 Center", "SAS": "Frost Bank Center",
        "TOR": "Scotiabank Arena", "UTA": "Delta Center", "WAS": "Capital One Arena"}


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def schedule_full(season: str) -> list[dict]:
    """리그 전체 일정(프리시즌 포함, 올스타 등 제외). 실패하면 저장본 + 자동 재시도 (resilient.py)."""
    def fetch():
        from nba_api.stats.endpoints import scheduleleaguev2
        df = RV._stats_call(lambda: scheduleleaguev2.ScheduleLeagueV2(season=season, timeout=30).get_data_frames()[0])
        out = []
        for r in df.itertuples():
            if r.gameId[:3] not in TYPES or not isinstance(r.homeTeam_teamTricode, str) or not r.homeTeam_teamTricode:
                continue
            out.append({"gid": r.gameId, "utc": r.gameDateTimeUTC, "et_date": str(r.gameDateEst)[:10],
                        "home": r.homeTeam_teamTricode, "away": r.awayTeam_teamTricode,
                        "arena": r.arenaName or "", "city": r.arenaCity or "", "status": int(r.gameStatus)})
        return sorted(out, key=lambda g: g["utc"])
    return LG._cached(f"schedule_full_{season}.json", fetch, timedelta(hours=6))


def venue(g: dict) -> tuple[tuple[float, float] | None, str]:
    """경기장 좌표. 좌표표에 없는 경기장이면 홈팀 경기장으로 대신하고 그렇게 적는다."""
    if g["arena"] in ARENAS:
        return ARENAS[g["arena"]], g["arena"]
    home = HOME.get(g["home"])
    return (ARENAS[home], f"{g['arena'] or '경기장 미상'} (좌표 미확인 → {home} 기준)") if home else (None, g["arena"] or "경기장 미상")


def _d(s: str) -> date:
    return date.fromisoformat(s)


def team_conditions(games: list[dict], gid: str, team: str) -> dict:
    """한 팀의 이번 경기 일정 조건."""
    mine = [g for g in games if team in (g["home"], g["away"])]
    i = next(k for k, g in enumerate(mine) if g["gid"] == gid)
    g = mine[i]
    day = _d(g["et_date"])
    here, here_name = venue(g)
    prev = mine[i - 1] if i > 0 else None
    out = {"team": team, "home": g["home"] == team, "arena": here_name, "date_et": g["et_date"], "prev": None,
           "rest_days": None, "b2b": False, "dense": [], "travel_km": None, "week": {}}
    if prev:
        pv, pv_name = venue(prev)
        out["rest_days"] = (day - _d(prev["et_date"])).days - 1
        out["b2b"] = out["rest_days"] == 0
        out["travel_km"] = round(haversine(pv, here)) if pv and here else None
        out["prev"] = {"date_et": prev["et_date"], "opp": prev["away"] if prev["home"] == team else prev["home"],
                       "home": prev["home"] == team, "arena": pv_name, "type": TYPES.get(prev["gid"][:3], "")}
    # 빡빡한 일정: 이번 경기를 포함해 4일 동안 3경기 / 6일 동안 4경기 / 7일 동안 5경기
    for span, need in ((4, 3), (6, 4), (7, 5)):
        n = sum(1 for x in mine[:i + 1] if (day - _d(x["et_date"])).days < span)
        if n >= need:
            out["dense"].append(f"{span}일 {n}경기")
    # 최근 7일: 이번 경기 전 7일 동안의 경기 수, 그 경기들과 이번 경기에 도착하기까지의 이동 거리 합
    week = [k for k in range(i) if 1 <= (day - _d(mine[k]["et_date"])).days <= 7]
    km, unknown = 0.0, False
    for k in week + [i]:
        if k == 0:
            continue
        a, b = venue(mine[k - 1])[0], venue(mine[k])[0]
        if a and b:
            km += haversine(a, b)
        else:
            unknown = True
    out["week"] = {"games": len(week), "km": round(km), "unknown": unknown,
                   "from": (day - timedelta(days=7)).isoformat(), "to": (day - timedelta(days=1)).isoformat()}
    return out


def build(gid: str, focus: str, tipoff_utc: str) -> dict:
    """양 팀 비교. 일정을 못 받았으면 RS.Unavailable."""
    tip = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    season = RV._season_of(tip)
    games = schedule_full(season)
    g = next((x for x in games if x["gid"] == gid), None)
    if not g:
        raise RS.Unavailable("NBA 공식 일정에서 이 경기를 찾지 못했습니다")
    opp = g["away"] if g["home"] == focus else g["home"]
    sides = {t: team_conditions(games, gid, t) for t in (focus, opp)}
    a, b = sides[focus], sides[opp]
    edges = []
    if a["rest_days"] is not None and b["rest_days"] is not None and a["rest_days"] != b["rest_days"]:
        who, d = (focus, a["rest_days"] - b["rest_days"]) if a["rest_days"] > b["rest_days"] else (opp, b["rest_days"] - a["rest_days"])
        edges.append({"team": who, "text": f"휴식 우위 {who} +{d}일"})
    if a["travel_km"] is not None and b["travel_km"] is not None and abs(a["travel_km"] - b["travel_km"]) >= 100:
        who = focus if a["travel_km"] < b["travel_km"] else opp
        edges.append({"team": who, "text": f"이동 부담 적음 {who} (직전 경기 뒤 이동 {abs(a['travel_km'] - b['travel_km']):,} km 짧음)"})
    for t in (focus, opp):
        if sides[t]["b2b"]:
            edges.append({"team": opp if t == focus else focus, "text": f"{t} 백투백"})
        elif sides[t]["dense"]:
            edges.append({"team": opp if t == focus else focus, "text": f"{t} 빡빡한 일정 ({', '.join(sides[t]['dense'])})"})
    if a["week"]["km"] - b["week"]["km"] >= 1000 or b["week"]["km"] - a["week"]["km"] >= 1000:
        who = focus if a["week"]["km"] < b["week"]["km"] else opp
        edges.append({"team": who, "text": f"최근 7일 이동 적음 {who} ({abs(a['week']['km'] - b['week']['km']):,} km 차)"})
    fail = RS.failing(f"schedule_full_{season}.json")
    return {"gid": gid, "season": season, "teams": sides, "order": [focus, opp], "edges": edges,
            "computed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "schedule_as_of": RS.as_of(LG._path(f"schedule_full_{season}.json")), "stale": fail,
            "source": SOURCE,
            "rule": "날짜는 미국 동부 기준. 휴식일 = 직전 경기와 이번 경기 사이의 빈 날 수 (0일 = 백투백). "
                    "이동 거리 = 경기장 좌표로 계산한 직선(하버사인) 거리이며 경기 사이 귀가는 셈하지 않음. "
                    "최근 7일 = 이번 경기 전 7일 동안의 경기 수와, 그 경기들과 이번 경기에 도착하기까지의 이동 거리 합. 프리시즌 포함."}
