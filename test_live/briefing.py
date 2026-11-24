"""'오늘의 브리핑' 데이터. test_live/briefing/ 에만 저장한다.

자동으로 갱신되는 부분 (ESPN 공개 API + NBA 박스스코어):
  - 부상자 현황 (결장/불투명)
  - 예상 선발: 직전 경기 선발에서 결장 확정 선수를 뺀 명단, 평소 선발(지난 시즌 마지막 경기)과 비교
  - 뉴스 목록 (ESPN 기사, 영상·의견 콘텐츠 제외)
사람(Claude)이 작성해 두는 부분 (briefing/editorial_<게임ID>.json):
  - 주요 뉴스 3줄 요약(한국어), 지켜볼 포인트 2개 — 각 항목에 출처 링크
  - 작성 이후 새로 나온 기사·부상 변동은 자동으로 골라 '작성 이후 새 소식'에 표시
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import re

import requests

import resilient as RS
import nba_http as NH

HERE = os.path.dirname(os.path.abspath(__file__))
BRIEF_DIR = os.path.join(HERE, "briefing")
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_HEADERS = {"User-Agent": "Mozilla/5.0"}

# ESPN 약어 → NBA 약어 (다른 것만)
ESPN_TO_NBA = {"NY": "NYK", "WSH": "WAS", "SA": "SAS", "GS": "GSW", "NO": "NOP", "UTAH": "UTA"}
NBA_TO_ESPN = {v: k for k, v in ESPN_TO_NBA.items()}
ESPN_IDS = {"ATL": 1, "BOS": 2, "BKN": 17, "CHA": 30, "CHI": 4, "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8, "GSW": 9,
            "HOU": 10, "IND": 11, "LAC": 12, "LAL": 13, "MEM": 29, "MIA": 14, "MIL": 15, "MIN": 16, "NOP": 3,
            "NYK": 18, "OKC": 25, "ORL": 19, "PHI": 20, "PHX": 21, "POR": 22, "SAC": 23, "SAS": 24, "TOR": 28,
            "UTA": 26, "WAS": 27}

STATUS_KO = {"out": "결장", "doubtful": "불투명", "questionable": "불투명", "day-to-day": "불투명",
             "game time decision": "불투명", "probable": "출전 유력"}
BODY_KO = {"Knee": "무릎", "Foot": "발", "Hip": "고관절", "Ankle": "발목", "Hamstring": "햄스트링",
           "Back": "허리", "Shoulder": "어깨", "Wrist": "손목", "Hand": "손", "Calf": "종아리",
           "Groin": "사타구니", "Achilles": "아킬레스건", "Illness": "질병", "Rest": "휴식", "Thumb": "엄지",
           "Finger": "손가락", "Elbow": "팔꿈치", "Concussion": "뇌진탕", "Toe": "발가락", "Quadriceps": "대퇴사두근",
           "Neck": "목", "Abdomen": "복부", "Oblique": "옆구리", "Shin": "정강이", "Thigh": "허벅지"}
SIDE_KO = {"Left": "왼쪽", "Right": "오른쪽"}
REASON_KO = {"Coach's Decision": "감독 결정", "Rest": "휴식", "Personal": "개인 사유", "Not With Team": "팀 미합류",
             "Suspension": "출장 정지", "Illness": "질병", "Return To Competition Reconditioning": "복귀 컨디셔닝",
             "G League": "G리그", "Two-Way": "투웨이 계약"}
DETAIL_KO = {"Bruise": "타박상", "Contusion": "타박상", "Sprain": "염좌", "Strain": "근육 염좌",
             "Surgery": "수술", "Fracture": "골절", "Soreness": "통증", "Tear": "파열", "Tendinitis": "건염",
             "Inflammation": "염증", "Personal": "개인 사유"}
# 뉴스 목록에서 뺄 것: 영상, 의견·토론성 콘텐츠, 루머성 제목
NEWS_TYPES = {"Story", "HeadlineNews", "Recap", "Preview"}
NEWS_EXCLUDE = ("rumor", "rumors", "sounds off", "hot take", "first take", "should the", "trade target",
                "mock", "fantasy")


def _get(url: str, _purpose: str | None = None, **params) -> dict:
    hdr = {**ESPN_HEADERS, NH.PURPOSE: _purpose} if _purpose else ESPN_HEADERS   # 용도: 라이브 중에도 보낼 요청 표시
    r = NH.SESSION.get(url, params=params, headers=hdr, timeout=20)   # 요청 관리자 (한도·쉬기·집계)
    r.raise_for_status()
    return r.json()


def _cache(name: str, fetch):
    os.makedirs(os.path.join(BRIEF_DIR, "cache"), exist_ok=True)
    path = os.path.join(BRIEF_DIR, "cache", name)
    data = RS._load(path)   # 없거나 깨진 파일이면 다시 받는다 (성공했을 때만 저장)
    return data if data is not None else RS.fetch(name, fetch, path, label="경기 정보")


# ---------------------------------------------------------------- 경기 찾기

def game_info(game_id: str) -> dict:
    """NBA 경기 ID → 날짜·팀 (stats 일정, 캐시)."""
    def fetch():
        from nba_api.stats.endpoints import scheduleleaguev2
        season = f"20{game_id[3:5]}-{int(game_id[3:5]) + 1:02d}"
        df = scheduleleaguev2.ScheduleLeagueV2(season=season, timeout=30).get_data_frames()[0]
        row = df[df["gameId"] == game_id].iloc[0]
        return {"game_id": game_id, "tipoff_utc": row["gameDateTimeUTC"],
                "home": row["homeTeam_teamTricode"], "away": row["awayTeam_teamTricode"]}
    return _cache(f"game_{game_id}.json", fetch)


def espn_event(info: dict) -> dict | None:
    utc = datetime.fromisoformat(info["tipoff_utc"].replace("Z", "+00:00"))
    et_date = (utc - timedelta(hours=4)).strftime("%Y%m%d")
    want = {NBA_TO_ESPN.get(info["home"], info["home"]), NBA_TO_ESPN.get(info["away"], info["away"])}
    for ev in _get(f"{ESPN}/scoreboard", dates=et_date).get("events", []):
        abbrs = {c["team"]["abbreviation"] for c in ev["competitions"][0]["competitors"]}
        if abbrs == want:
            return {"id": ev["id"], "name": ev["shortName"], "date": ev["date"],
                    "status": ev["status"]["type"]["description"]}
    return None


# ---------------------------------------------------------------- 부상자

def injuries(event_id: str, summary: dict | None = None) -> dict[str, list[dict]]:
    """ESPN 경기 요약의 부상자 목록. summary 를 주면(라이브 추적이 방금 받은 것) 따로 요청하지 않는다."""
    out: dict[str, list[dict]] = {}
    if summary is None:
        summary = _get(f"{ESPN}/summary", _purpose="injuries", event=event_id)
    for team in summary.get("injuries", []):
        tri = ESPN_TO_NBA.get(team["team"]["abbreviation"], team["team"]["abbreviation"])
        rows = []
        for i in team.get("injuries", []):
            d = i.get("details", {}) or {}
            status = (i.get("status") or "").strip()
            fantasy = (d.get("fantasyStatus") or {}).get("description", "")
            typ = d.get("type") or ""
            if typ in REASON_KO:
                part = REASON_KO[typ]
            else:
                part = " ".join(x for x in (SIDE_KO.get(d.get("side"), ""), BODY_KO.get(typ, typ),
                                            DETAIL_KO.get(d.get("detail"), d.get("detail") or "")) if x)
            rows.append({
                "name": i["athlete"].get("displayName"), "short": i["athlete"].get("shortName"),
                "espn_id": i["athlete"].get("id"),
                "status": STATUS_KO.get(status.lower(), status), "status_raw": status, "tag": fantasy,
                "part": part, "return_date": d.get("returnDate"), "updated": i.get("date"),
                "link": f"https://www.espn.com/nba/team/injuries/_/name/{team['team']['abbreviation'].lower()}",
            })
        out[tri] = rows
    return out


# ---------------------------------------------------------------- 선발

def _starters(game_id: str, tri: str) -> list[dict]:
    from nba_api.live.nba.endpoints import boxscore
    from tracker import BROWSER_HEADERS  # 같은 폴더의 tracker.py

    try:
        box = _cache(f"box_{game_id}.json",
                     lambda: boxscore.BoxScore(game_id, headers=BROWSER_HEADERS, timeout=30).get_dict()["game"])
    except Exception:   # NBA(cdn) 가 막혔으면 ESPN 경기 요약 (요청 관리자가 끝난 경기 요약을 영구 저장)
        import espn_source
        box = espn_source.final_game(game_id)["box"]
    for side in ("homeTeam", "awayTeam"):
        if box[side]["teamTricode"] == tri:
            return [{"name": p["nameI"], "pos": p.get("position", "")} for p in box[side]["players"] if p.get("starter") == "1"]
    return []


def _team_games(tri: str, season: str, season_type: str) -> list[dict]:
    from nba_api.stats.endpoints import teamgamelogs
    from nba_api.stats.static import teams
    tid = teams.find_team_by_abbreviation(tri)["id"]
    df = teamgamelogs.TeamGameLogs(team_id_nullable=tid, season_nullable=season,
                                   season_type_nullable=season_type, timeout=30).get_data_frames()[0]
    return [{"game_id": r.GAME_ID, "date": r.GAME_DATE[:10], "matchup": r.MATCHUP}
            for r in df.sort_values("GAME_DATE").itertuples()]


def lineup(info: dict, focus: str, inj: list[dict]) -> dict:
    """예상 선발 = 직전 경기 선발 − 결장 확정. 평소 선발 = 지난 시즌 마지막 경기 선발."""
    season_start = int("20" + info["game_id"][3:5])
    season = f"{season_start}-{(season_start + 1) % 100:02d}"
    prev_season = f"{season_start - 1}-{season_start % 100:02d}"
    tip = info["tipoff_utc"][:10]
    recent = [g for t in ("Pre Season", "Regular Season") for g in _team_games(focus, season, t) if g["date"] < tip]
    recent.sort(key=lambda g: g["date"])
    last_prev = (_team_games(focus, prev_season, "Playoffs") or _team_games(focus, prev_season, "Regular Season"))[-1]
    last = recent[-1] if recent else last_prev

    base_full = _starters(last["game_id"], focus)
    pos_of = {p["name"]: p["pos"] for p in base_full}
    base = [p["name"] for p in base_full]
    usual = [p["name"] for p in _starters(last_prev["game_id"], focus)]
    by_short = {i["short"]: i for i in inj}
    expected, notes = [], []
    for name in base:
        i = by_short.get(name)
        if i and i["status"] == "결장":
            notes.append({"text": f"{name} 결장 확정({i['part']}) → 선발 제외, 대체 선수는 팀 발표 전까지 미정",
                          "link": i["link"]})
            continue
        expected.append({"name": name, "pos": pos_of.get(name, ""), "flag": f"출전 {i['status']}" if i else ""})
        if i:
            notes.append({"text": f"{name} {i['status']}({i['part']}) — 결장하면 선발이 바뀔 수 있음", "link": i["link"]})
    exp_names = {e["name"] for e in expected}
    for name in usual:
        if name not in exp_names and not any(name in n["text"] for n in notes):
            notes.append({"text": f"{name}: 평소 선발이지만 직전 경기 선발에서 빠짐",
                          "link": f"https://www.nba.com/game/{last['game_id']}/box-score"})
    for e in expected:
        if e["name"] not in usual:
            notes.append({"text": f"{e['name']}: 평소에는 벤치, 직전 경기 선발",
                          "link": f"https://www.nba.com/game/{last['game_id']}/box-score"})
    if not notes:
        notes.append({"text": "평소 선발과 같음", "link": f"https://www.nba.com/game/{last['game_id']}/box-score"})
    return {"expected": expected, "usual": usual, "notes": notes,
            "basis": {"last_game": last, "usual_game": last_prev},
            "links": {"last": f"https://www.nba.com/game/{last['game_id']}/box-score",
                      "usual": f"https://www.nba.com/game/{last_prev['game_id']}/box-score"}}


# ---------------------------------------------------------------- 뉴스

def news(team_espn_id: str = "18", hours: int = 96) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for a in _get(f"{ESPN}/news", team=team_espn_id, limit=25).get("articles", []):
        head = a.get("headline", "")
        if a.get("type") not in NEWS_TYPES or any(k in head.lower() for k in NEWS_EXCLUDE):
            continue
        pub = datetime.fromisoformat(a["published"].replace("Z", "+00:00"))
        if pub < since:
            continue
        out.append({"id": a["id"], "headline": head, "description": a.get("description", ""),
                    "published": a["published"], "source": "ESPN",
                    "link": a.get("links", {}).get("web", {}).get("href"),
                    "team_tags": [c.get("teamId") for c in a.get("categories", []) if c.get("type") == "team"]})
    return out


TEAM_ALIASES = {"PHI": ["Sixers"], "NYK": [], "POR": ["Blazers"], "MIN": ["Wolves"], "OKC": [], "LAL": [], "LAC": []}
NEWS_ROUNDUP_TEAMS = 6   # 팀 태그가 이만큼 많으면 리그 전체 정리 기사로 본다


def game_news(teams: list[str], hours: int = 96) -> list[dict]:
    """오늘 경기 두 팀과 관련된 기사만: 제목·요약에 팀 이름이나 그 팀 선수 이름이 나오고, 리그 전체 정리 기사는 뺀다."""
    from nba_api.stats.static import teams as st
    import rivals
    words: dict[str, str] = {}
    for tri in teams:
        t = st.find_team_by_abbreviation(tri)
        for w in [t["nickname"], t["full_name"], *TEAM_ALIASES.get(tri, [])]:
            words[w] = tri
        try:
            season = rivals._season_of(datetime.now(timezone.utc))
            for p in rivals._roster(tri, season):
                words[p["name"]] = tri
        except Exception:
            pass
    seen, out = set(), []
    for tri in teams:
        for n in news(str(ESPN_IDS.get(tri, 18)), hours):
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            text = f"{n['headline']} {n['description']}"
            hit = sorted({words[w] for w in words if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", text)})
            if not hit or len(n.get("team_tags", [])) >= NEWS_ROUNDUP_TEAMS:
                continue
            out.append({**n, "teams": hit})
    out.sort(key=lambda n: n["published"], reverse=True)
    return out


# ---------------------------------------------------------------- 제목 번역

TRANSLATIONS = os.path.join(BRIEF_DIR, "translations.json")   # 기사 ID → 한국어 제목 (사람 번역 우선)


def translate_titles(items: list[dict]) -> list[dict]:
    cache = {}
    if os.path.exists(TRANSLATIONS):
        with open(TRANSLATIONS) as f:
            cache = json.load(f)
    changed = False
    for n in items:
        key = str(n["id"])
        if key not in cache:
            ko = _machine_translate(n["headline"])
            if ko:
                cache[key] = {"ko": ko, "by": "MyMemory 자동 번역"}
                changed = True
        if key in cache:
            n["headline_ko"], n["translated_by"] = polish_ko(cache[key]["ko"]), cache[key]["by"]
    if changed:
        os.makedirs(BRIEF_DIR, exist_ok=True)
        with open(TRANSLATIONS, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    return items


_PARTICLE = {"은": ("은", "는"), "는": ("은", "는"), "이": ("이", "가"), "가": ("이", "가"), "을": ("을", "를"),
             "를": ("을", "를"), "과": ("과", "와"), "와": ("과", "와"), "으로": ("으로", "로"), "로": ("으로", "로")}


def _batchim(word: str) -> bool:
    ch = word[-1]
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    return False


def _josa(word: str, p: str) -> str:
    if p not in _PARTICLE:
        return p
    a, b = _PARTICLE[p]
    if a == "으로":   # ㄹ 받침은 '로'
        return "로" if not _batchim(word) or (ord(word[-1]) - 0xAC00) % 28 == 8 else "으로"
    return a if _batchim(word) else b


def polish_ko(ko: str) -> str:
    """기계 번역 다듬기: 존칭 '님' 빼기, '엠비드 은 (는)' → '엠비드는', 이름 뒤 띄어 쓴 조사 붙이기."""
    ko = strip_honorific(ko)
    ko = re.sub(r"([가-힣]+)\s*(은|는|이|가|을|를|과|와)\s*\((?:은|는|이|가|을|를|과|와)\)",
                lambda m: m.group(1) + _josa(m.group(1), m.group(2)), ko)
    # 띄어 쓴 조사 '앤서니 는' → '앤서니는' (은·는·을·를·과·와는 단독 낱말로 쓰이지 않는다; '이'는 '이 경기'처럼 쓰여 제외)
    ko = re.sub(r"([가-힣]+)\s+(은|는|을|를|과|와)(?=\s)", lambda m: m.group(1) + _josa(m.group(1), m.group(2)), ko)
    names = set()
    if os.path.exists(NAMES_KO):
        with open(NAMES_KO) as f:
            g = json.load(f)
        names = {v for k in ("full", "teams", "surname") for v in g.get(k, {}).values()}
    for n in sorted(names, key=len, reverse=True):
        ko = re.sub(re.escape(n) + r"\s+(은|는|이|가|을|를|의|과|와|도|로|으로|에게)(?=[\s,.!?'\"’”]|$)",
                    lambda m: n + (_josa(n, m.group(1)) if m.group(1) in _PARTICLE else m.group(1)), ko)
    return ko


def strip_honorific(ko: str) -> str:
    """기사 제목 번역의 '님' 존칭을 뺀다 ('브런슨 님은' → '브런슨은', '감독님' → '감독')."""
    ko = re.sub(r"(?<=[가-힣\]])\s?님(?=[\s,.!?:;'\"’”)\]]|$|[이은을의께과와도이가에])", "", ko)
    return re.sub(r"\s{2,}", " ", ko).strip()


NAMES_KO = os.path.join(BRIEF_DIR, "names_ko.json")   # 선수 이름 표준 한글 표기


def _protect_names(text: str) -> tuple[str, list[str]]:
    """선수 이름을 [0], [1] … 표시로 바꿔 번역기가 건드리지 못하게 하고, 되돌릴 한글 이름 목록을 돌려준다."""
    import re
    if not os.path.exists(NAMES_KO):
        return text, []
    with open(NAMES_KO) as f:
        g = json.load(f)
    pairs = (sorted(g.get("full", {}).items(), key=lambda kv: -len(kv[0])) + sorted(g.get("teams", {}).items(), key=lambda kv: -len(kv[0]))
             + sorted(g.get("surname", {}).items(), key=lambda kv: -len(kv[0])))
    ko_list: list[str] = []
    for en, ko in pairs:
        pat = re.compile(r"(?<![\w-])" + re.escape(en) + r"(?![\w-])")
        if pat.search(text):
            text = pat.sub(f"[{len(ko_list)}]", text)
            ko_list.append(ko)
    return text, ko_list


def _machine_translate(text: str) -> str | None:
    """MyMemory 무료 번역 API (공개 기사 제목만 보낸다). 선수 이름은 표준 한글 표기로 지킨다. 실패하면 None → 원문."""
    import re
    try:
        masked, names = _protect_names(text)
        r = requests.get("https://api.mymemory.translated.net/get", params={"q": masked, "langpair": "en|ko"}, timeout=10)
        r.raise_for_status()
        ko = r.json().get("responseData", {}).get("translatedText", "")
        if not ko or "MYMEMORY WARNING" in ko or ko.strip().lower() == masked.strip().lower():
            return None
        ko = strip_honorific(ko)
        for i, name in enumerate(names):
            ko = ko.replace(f"[{i}]", name)
        if re.search(r"\[\d+\]", ko):        # 표시가 남았으면(번역기가 망가뜨림) 믿지 않는다
            return None
        return re.sub(r"\s*\([A-Za-z][^)]*\)", "", ko).strip()   # '(Karl-Anthony Towns)' 같은 영어 괄호 제거
    except Exception:
        return None


# ---------------------------------------------------------------- 조립

def build(game_id: str, focus: str = "NYK", on_recover=None) -> dict:
    """자동 갱신 부분을 새로 받아 편집본과 합친다.
    부상·뉴스는 실패하면 마지막으로 받은 것을 쓰고(stale 에 기준 시각), 30초·1분·5분 뒤 다시 받아 on_recover 를 부른다.
    에러 원문은 logs/fetch_errors.log 에만 남기고 errors 에는 화면용 짧은 문구만 넣는다."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    info = game_info(game_id)
    opp = info["away"] if info["home"] == focus else info["home"]
    brief = {"game_id": game_id, "focus": focus, "opponent": opp, "tipoff_utc": info["tipoff_utc"],
             "refreshed_utc": now, "errors": [], "injuries": {}, "lineup": None, "news_feed": []}
    cache = os.path.join(BRIEF_DIR, "cache")

    def fetch_injuries():
        ev = espn_event(info)
        return {"event": ev, "injuries": injuries(ev["id"]) if ev else {}}
    try:
        inj = RS.fetch(f"injuries_{game_id}", fetch_injuries, os.path.join(cache, f"last_injuries_{game_id}.json"),
                       on_recover=on_recover)
        brief["espn_event"], brief["injuries"] = inj["event"], inj["injuries"]
    except RS.Unavailable as exc:
        brief["errors"].append(str(exc))
    try:
        brief["lineup"] = lineup(info, focus, brief["injuries"].get(focus, []))
        brief["lineup_opp"] = opp_lineup(info, opp, brief["injuries"].get(opp, []))
    except Exception as exc:
        RS.log_error("브리핑 선발", exc, game_id)
        brief["errors"].append("직전 경기 선발을 받지 못했습니다 (다음 갱신 때 다시 시도)")
    try:
        brief["news_feed"] = RS.fetch(f"news_{game_id}", lambda: translate_titles(game_news([focus, opp])),
                                      os.path.join(cache, f"last_news_{game_id}.json"), on_recover=on_recover)
    except RS.Unavailable as exc:
        brief["errors"].append(str(exc))
    brief["stale"] = [n for n in (RS.failing(f"injuries_{game_id}"), RS.failing(f"news_{game_id}")) if n]

    ed_path = os.path.join(BRIEF_DIR, f"editorial_{game_id}.json")
    if os.path.exists(ed_path):
        with open(ed_path) as f:
            ed = json.load(f)
        brief["editorial"] = ed
        seen = set(ed.get("seen_news_ids", []))
        brief["new_since_editorial"] = [n for n in brief["news_feed"] if n["id"] not in seen]
        before = {(i["name"], i["status"]) for t in ed.get("seen_injuries", {}).values() for i in t}
        brief["injury_changes"] = [
            {**i, "team": t} for t, rows in brief["injuries"].items() for i in rows if (i["name"], i["status"]) not in before]
    os.makedirs(BRIEF_DIR, exist_ok=True)
    with open(os.path.join(BRIEF_DIR, f"brief_{game_id}.json"), "w") as f:
        json.dump(brief, f, ensure_ascii=False, indent=1)
    return brief


def opp_lineup(info: dict, opp: str, inj: list[dict]) -> dict | None:
    """상대팀 예상 선발: 상대의 직전 경기 선발에서 결장 확정 선수를 뺀다."""
    season_start = int("20" + info["game_id"][3:5])
    season = f"{season_start}-{(season_start + 1) % 100:02d}"
    prev = f"{season_start - 1}-{season_start % 100:02d}"
    tip = info["tipoff_utc"][:10]
    games = [g for t in ("Pre Season", "Regular Season", "Playoffs") for g in _team_games(opp, season, t) if g["date"] < tip]
    if not games:
        games = _team_games(opp, prev, "Playoffs") or _team_games(opp, prev, "Regular Season")
    if not games:
        return None
    last = sorted(games, key=lambda g: g["date"])[-1]
    out = {i["short"] for i in inj if i["status"] == "결장"}
    exp = [{"name": p["name"], "pos": p["pos"], "flag": ""} for p in _starters(last["game_id"], opp) if p["name"] not in out]
    return {"expected": exp, "basis": {"last_game": last}}


def lineup_diff(expected: list[str], actual: list[str]) -> dict | None:
    """실제 선발이 예상과 다르면 차이를, 같으면 None."""
    exp, act = set(expected), set(actual)
    if exp == act:
        return None
    return {"in": sorted(act - exp), "out": sorted(exp - act)}


if __name__ == "__main__":
    import sys
    b = build(sys.argv[1] if len(sys.argv) > 1 else "0012600035")
    print(json.dumps({k: b[k] for k in ("injuries", "lineup", "errors")}, ensure_ascii=False, indent=1)[:3000])
    print("뉴스", len(b["news_feed"]), "건")
