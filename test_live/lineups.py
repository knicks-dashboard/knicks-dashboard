"""선발 라인업 예측(출처 기반) · 유닛 구분 · 스토리 배지.

선발 출처 우선순위 (직접 추측하지 않는다)
  1) 공식: NBA 공식 박스스코어에 선발이 올라오면 즉시 교체 ('공식 확정')
  2) 기사·전문 사이트: 팁오프 7일 이내 자료만 — 다수결, 다른 의견도 표시
       · 자동: ESPN 뎁스 차트(포지션별 1순위), 로토와이어 예상/확정 선발(경기 당일 페이지)
       · 웹 검색으로 찾은 기사: briefing/lineup_sources_<게임ID>.json (출처 링크·날짜 포함)
  3) 위 둘이 없을 때만: 지난 정규시즌 선발 출전 수 상위 5명 (프리시즌 선발은 근거로 쓰지 않는다)
유닛: ESPN 뎁스 차트 순서 + 지난 시즌 경기당 출전 시간
  · 뎁스 차트를 못 받으면 마지막으로 받은 뎁스 차트, 그것도 없으면 지난 시즌 출전 시간 순서('추정')
배지: 공식 기록(stats.nba.com)·기사 근거만, 근거 링크 포함
모든 쓰기는 test_live/ 안에서만 (rivals_cache/, briefing/).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

import briefing
import resilient as RS
import nba_http as NH
import rivals as RV
import tracker as T

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "rivals_cache", "lineups")
ESPN_TEAM_ID = {"ATL": 1, "BOS": 2, "BKN": 17, "CHA": 30, "CHI": 4, "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8, "GSW": 9,
                "HOU": 10, "IND": 11, "LAC": 12, "LAL": 13, "MEM": 29, "MIA": 14, "MIL": 15, "MIN": 16, "NOP": 3,
                "NYK": 18, "OKC": 25, "ORL": 19, "PHI": 20, "PHX": 21, "POR": 22, "SAC": 23, "SAS": 24, "TOR": 28,
                "UTA": 26, "WAS": 27}
ROTO_ABBR = {"NYK": "NY", "SAS": "SA", "GSW": "GS", "NOP": "NO", "PHX": "PHO", "WAS": "WAS", "UTA": "UTA", "BKN": "BKN"}
ARTICLE_DAYS = 7
MSG_MIN_GAMES = 5     # MSG 강자: MSG 정규시즌 5경기 이상
MSG_MIN_GAP = 1.0     # 커리어 평균보다 1점 이상 높을 때만 (0.1점 차 같은 우연은 빼기)
POS = ("pg", "sg", "sf", "pf", "c")
H = {"User-Agent": "Mozilla/5.0"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def name_i(full: str) -> str:
    """'Karl-Anthony Towns' → 'K. Towns' (NBA 박스스코어 nameI 형식, 실제 선발과 비교할 때 쓴다)."""
    parts = full.split()
    return f"{parts[0][0]}. {' '.join(parts[1:])}" if len(parts) > 1 else full


def _cached(name: str, fetch, max_age: timedelta | None):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and (max_age is None or time.time() - os.path.getmtime(path) < max_age.total_seconds()):
        with open(path) as f:
            return json.load(f)
    return RS.fetch(name, fetch, path)   # 실패하면 저장본 + 자동 재시도 (resilient.py)


# ---------------------------------------------------------------- 출처 1: 공식

def official_starters(game_id: str) -> dict[str, list[dict]] | None:
    """NBA 공식 박스스코어(cdn)에 양 팀 선발 5명씩 올라와 있으면 돌려준다. 팁오프 전에는 보통 없다."""
    try:
        # cdn.nba.com 은 라이브 중인 닉스 경기만 받는다 (요청 관리자 정책) → 경기 전에는 묻지 않음
        g = T.cdn_get(f"boxscore/boxscore_{game_id}.json", "live" if NH.is_live() else None)["game"]
    except Exception:
        return None
    out = {}
    for side in ("homeTeam", "awayTeam"):
        t = g[side]
        st = [{"id": p["personId"], "name": f"{p.get('firstName', '')} {p.get('familyName', '')}".strip() or p["name"],
               "short": p.get("nameI"), "pos": p.get("position", "")} for p in t.get("players", []) if p.get("starter") == "1"]
        if len(st) != 5:
            return None
        out[t["teamTricode"]] = st
    return out


# ---------------------------------------------------------------- 출처 2: ESPN 뎁스 차트 · 로토와이어 · 기사 파일

def espn_depth(tri: str) -> dict:
    d = NH.SESSION.get(f"{briefing.ESPN}/teams/{ESPN_TEAM_ID[tri]}/depthcharts", headers=H, timeout=20).json()
    pos = {p: [{"name": a["displayName"], "injured": [i.get("status") for i in a.get("injuries", [])]}
               for a in d["depthchart"][0]["positions"].get(p, {}).get("athletes", [])] for p in POS}
    return {"positions": pos, "checked_utc": d.get("timestamp"),
            "url": f"https://www.espn.com/nba/team/depth/_/name/{briefing.NBA_TO_ESPN.get(tri, tri).lower()}"}


def depth_starters(dc: dict) -> list[str]:
    """포지션별 1순위 (같은 선수가 두 포지션 1순위면 다음 순서)."""
    picked = []
    for p in POS:
        for a in dc["positions"][p]:
            if a["name"] not in picked:
                picked.append(a["name"])
                break
    return picked[:5]


def rotowire(home: str, away: str) -> dict | None:
    """로토와이어 오늘 경기 페이지에 이 경기가 있으면 예상/확정 선발과 결장 예상 선수."""
    t = requests.get("https://www.rotowire.com/basketball/nba-lineups.php", headers=H, timeout=20).text
    ha, aa = ROTO_ABBR.get(home, home), ROTO_ABBR.get(away, away)
    for block in t.split('class="lineup is-nba')[1:]:
        abbrs = re.findall(r'class="lineup__abbr">([A-Z]+)<', block)[:2]
        if abbrs != [aa, ha]:
            continue
        out = {}
        for side, tri in (("is-visit", away), ("is-home", home)):
            m = re.search(rf'lineup__list {side}">(.*?)</ul>', block, re.S)
            if not m:
                continue
            ul = m.group(1)
            status = re.search(r'lineup__status[^"]*"[^>]*>.*?</div>\s*([^<]+)<', ul, re.S)
            names = re.findall(r'<li class="lineup__player[^"]*"[^>]*>\s*<div class="lineup__pos"[^>]*>[A-Z]+</div>\s*<a title="([^"]+)"', ul)
            inj = re.findall(r'title="([^"]+)" href="/basketball/player/[^"]+">[^<]+</a>\s*<span class="lineup__inj">([A-Za-z-]+)<', ul)
            out[tri] = {"starters": names[:5], "status": (status.group(1).strip() if status else ""),
                        "may_not_play": [{"name": n, "tag": s} for n, s in inj]}
        return out or None
    return None


def article_sources(game_id: str) -> dict:
    path = os.path.join(briefing.BRIEF_DIR, f"lineup_sources_{game_id}.json")
    if not os.path.exists(path):
        return {"sources": [], "notes": [], "replacements": []}
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------- 공식 기록 (stats.nba.com)

def season_stats(season: str) -> dict[int, dict]:
    """지난 시즌 선수별 소속·경기 수·경기당 출전 시간 (끝난 시즌은 영구 캐시)."""
    def fetch():
        from nba_api.stats.endpoints import leaguedashplayerstats
        df = RV._stats_call(lambda: leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed="PerGame",
                                                                                 timeout=30).get_data_frames()[0])
        return {str(int(r.PLAYER_ID)): {"team": r.TEAM_ABBREVIATION, "gp": int(r.GP), "min": round(float(r.MIN), 1)}
                for r in df.itertuples()}
    data = _cached(f"season_{season}.json", fetch, None)
    return {int(k): v for k, v in data.items()}


def career_games(pid: int, season_type: str) -> list[dict]:
    def fetch():
        from nba_api.stats.endpoints import leaguegamefinder
        df = RV._stats_call(lambda: leaguegamefinder.LeagueGameFinder(player_or_team_abbreviation="P", player_id_nullable=pid,
                                                                       season_type_nullable=season_type, timeout=30).get_data_frames()[0])
        time.sleep(0.6)
        return [{"season": r.SEASON_ID, "team": r.TEAM_ABBREVIATION, "game_id": r.GAME_ID, "date": r.GAME_DATE,
                 "matchup": r.MATCHUP, "pts": int(r.PTS) if r.PTS == r.PTS else 0} for r in df.itertuples()]
    return _cached(f"games_{pid}_{season_type.replace(' ', '')}.json", fetch, timedelta(days=1))


def games_started(pid: int, season: str) -> int:
    def fetch():
        from nba_api.stats.endpoints import playercareerstats
        df = RV._stats_call(lambda: playercareerstats.PlayerCareerStats(player_id=pid, timeout=30).get_data_frames()[0])
        time.sleep(0.6)
        return {"rows": [{"season": r.SEASON_ID, "team": r.TEAM_ABBREVIATION, "gs": int(r.GS) if r.GS == r.GS else 0}
                         for r in df.itertuples()]}
    rows = _cached(f"career_{pid}.json", fetch, timedelta(days=7))["rows"]
    # 시즌 중 이적하면 팀별 줄과 합계(TOT) 줄이 함께 온다 → 합계가 있으면 합계만
    rs = [r for r in rows if r["season"] == season]
    tot = [r for r in rs if r["team"] == "TOT"]
    return (tot or rs or [{"gs": 0}])[0]["gs"] if (tot or rs) else 0


def _sid(season: str, kind: int) -> str:
    return f"{kind}{season[:4]}"


def _season_label(sid: str) -> str:
    y = int(sid[1:])
    return f"{y}-{(y + 1) % 100:02d}"


# ---------------------------------------------------------------- 스토리 배지

def badges(p: dict, team: str, focus: str, cur: str, prev_stats: dict[int, dict]) -> list[dict]:
    pid, out = p["id"], []
    link = f"https://www.nba.com/player/{pid}"
    rs = career_games(pid, "Regular Season")
    po = career_games(pid, "Playoffs")
    prev = RV._prev(cur)
    # 🆕 이적생: 지난 시즌 마지막 소속이 지금 팀과 다르다 (신인은 지난 시즌 기록이 없어 제외)
    last = prev_stats.get(pid)
    if last and last["team"] != team:
        n = sum(1 for g in rs if g["season"] == _sid(cur, 2) and g["team"] == team) + 1
        out.append({"icon": "🆕", "label": "이적생", "text": f"전 소속 {last['team']} ({prev} {last['gp']}경기) · 이적 후 정규시즌 {n}번째 경기",
                    "links": [{"name": "NBA.com 선수 기록", "url": link}]})
    if team != focus:
        # 🏠 친정 방문: 예전 닉스 소속
        ny = sorted({_season_label(g["season"]) for g in rs + po if g["team"] == focus})
        if ny:
            span = ny[0] if len(ny) == 1 else f"{ny[0]}~{ny[-1]}"
            out.append({"icon": "🏠", "label": "친정 방문", "text": f"{span} 닉스 소속 ({len(ny)}시즌)",
                        "links": [{"name": "NBA.com 선수 기록", "url": link}]})
        # ⚔️ 악연: 최근 3시즌 플레이오프 맞대결, 닉스 상대 퇴장·플래그런트
        seasons3 = [cur, prev, RV._prev(prev)]
        sids3 = {_sid(s, 4) for s in seasons3}
        vs = lambda g: g["matchup"].split()[-1] == focus and g["team"] != focus   # 닉스를 상대한 경기
        po_vs = [g for g in po if g["season"] in sids3 and vs(g)]
        lines, links = [], []
        by_season: dict[str, list] = {}
        for g in po_vs:
            by_season.setdefault(g["season"], []).append(g)
        for sid, gs in sorted(by_season.items(), reverse=True):
            gs.sort(key=lambda g: g["date"])
            lines.append(f"{int(sid[1:]) + 1} 플레이오프 맞대결 {len(gs)}경기 (당시 {gs[0]['team']})")
            links.append({"name": f"{int(sid[1:]) + 1} PO 경기", "url": f"https://www.nba.com/game/{gs[0]['game_id']}"})
        rough_games = [g for g in rs if g["season"] in {_sid(s, 2) for s in seasons3} and vs(g)] + po_vs
        for g in rough_games:
            try:
                evs = RV.rough_events(g["game_id"])
            except Exception:
                continue
            for e in evs:
                if e["pid"] == pid and e["kind"] in ("flagrant1", "flagrant2", "ejection"):
                    what = {"flagrant1": "플래그런트 1", "flagrant2": "플래그런트 2", "ejection": "퇴장"}[e["kind"]]
                    lines.append(f"닉스 상대 {what} ({g['date']}, {e['label']})")
                    links.append({"name": f"{g['date']} 경기", "url": f"https://www.nba.com/game/{g['game_id']}"})
        if lines:
            out.append({"icon": "⚔️", "label": "악연", "text": " · ".join(lines), "links": links[:4]})
    # 🔥 MSG 강자: MSG(닉스 홈) 정규시즌 평균 득점 > 커리어 정규시즌 평균 (5경기 이상)
    msg = [g for g in rs if g["matchup"].endswith(f"@ {focus}") or g["matchup"].startswith(f"{focus} vs.")]
    if len(msg) >= MSG_MIN_GAMES and rs:
        m_avg, c_avg = sum(g["pts"] for g in msg) / len(msg), sum(g["pts"] for g in rs) / len(rs)
        if m_avg >= c_avg + MSG_MIN_GAP:
            out.append({"icon": "🔥", "label": "MSG 강자", "text": f"MSG {m_avg:.1f}점 ({len(msg)}경기) · 커리어 {c_avg:.1f}점 (정규시즌)",
                        "links": [{"name": "NBA.com 선수 기록", "url": link}]})
    return out


# ---------------------------------------------------------------- 조립

def team_lineup(tri: str, focus: str, game: dict, cur: str, official: dict | None, roto: dict | None,
                art: dict, inj: list[dict], prev_stats: dict[int, dict], on_recover=None) -> dict:
    roster = RV._roster(tri, cur)
    by_norm = {norm(p["name"]): p for p in roster}
    tip = datetime.fromisoformat(game["tipoff_utc"].replace("Z", "+00:00"))
    errors = []

    def find(name: str) -> dict | None:
        return by_norm.get(norm(name))

    # 뎁스 차트: 실패하면 마지막으로 받은 것 (resilient.py 가 30초·1분·5분 뒤 다시 받아 on_recover 로 다시 계산)
    depth_key = f"depth_{tri}"
    try:
        dc = RS.fetch(depth_key, lambda: espn_depth(tri), os.path.join(CACHE, f"depth_{tri}.json"), on_recover=on_recover)
    except RS.Unavailable:
        dc = None
    fail = RS.failing(depth_key)
    depth_status = {"ok": fail is None, "as_of": fail["as_of"] if fail and dc else None,
                    "retrying": bool(fail and fail["retrying"]), "estimate": dc is None}

    # 출처 목록 (2순위)
    sources = []
    if dc:
        sources.append({"name": "ESPN 뎁스 차트", "kind": "전문 사이트", "url": dc["url"], "date": (dc["checked_utc"] or "")[:10],
                        "date_note": "ESPN 확인 시각", "starters": depth_starters(dc)})
    if roto and roto.get(tri):
        r = roto[tri]
        sources.append({"name": f"로토와이어 ({'확정' if 'Confirmed' in r['status'] else '예상'} 선발)", "kind": "전문 사이트",
                        "url": "https://www.rotowire.com/basketball/nba-lineups.php", "date": datetime.now(timezone.utc).date().isoformat(),
                        "date_note": "오늘 확인", "starters": r["starters"]})
    old = []
    for s in art.get("sources", []):
        if tri not in s.get("starters", {}):
            continue
        pub = datetime.fromisoformat(s["published"] + ("T00:00:00+00:00" if len(s["published"]) == 10 else ""))
        item = {"name": s["name"], "kind": s.get("kind", "기사"), "url": s["url"], "date": s["published"][:10],
                "date_note": s.get("date_note", ""), "author": s.get("author", ""), "starters": s["starters"][tri]}
        (sources if tip - pub <= timedelta(days=ARTICLE_DAYS) else old).append(item)

    injured = {norm(i["name"]): i for i in inj if i["status"] in ("결장", "불투명")}

    def player(name: str, grade: str, votes: int = 0, total: int = 0) -> dict:
        p = find(name)
        i = injured.get(norm(name))
        return {"id": p["id"] if p else None, "name": p["name"] if p else name, "short": name_i(p["name"] if p else name),
                "pos": (p or {}).get("pos", ""), "grade": grade, "votes": votes, "total": total,
                "injury": {"status": i["status"], "part": i["part"], "link": i["link"]} if i else None}

    level, starters, dissent, basis = None, [], [], ""
    if official and official.get(tri):
        level = "official"
        starters = [player(p["name"], "공식") for p in official[tri]]
        basis = "NBA 공식 박스스코어에 발표된 선발"
    elif sources:
        level = "article"
        votes: dict[str, int] = {}
        order: dict[str, int] = {}
        for s in sources:
            for k, n in enumerate(s["starters"]):
                key = norm(n)
                votes[key] = votes.get(key, 0) + 1
                order[key] = min(order.get(key, 99), k)   # 동률이면 출처들이 앞에 적은 선수
        names = {norm(n): n for s in sources for n in s["starters"]}
        top = sorted(votes, key=lambda k: (-votes[k], order[k]))[:5]
        starters = [player(names[k], "기사", votes[k], len(sources)) for k in top]
        chosen = set(top)
        for s in sources:
            extra = [n for n in s["starters"] if norm(n) not in chosen]
            missing = [names[k] for k in top if k not in {norm(n) for n in s["starters"]}]
            if extra:
                dissent.append({"source": s["name"], "url": s["url"], "date": s["date"],
                                "text": f"{', '.join(extra)} 선발" + (f" ({', '.join(missing)} 대신)" if missing else "")})
        basis = f"팁오프 {ARTICLE_DAYS}일 이내 출처 {len(sources)}곳 다수결"
    else:
        level = "estimate"
        gs = sorted(((games_started(p["id"], RV._prev(cur)), (prev_stats.get(p["id"]) or {}).get("min", 0), p) for p in roster),
                    key=lambda x: (-x[0], -x[1]))
        starters = [{**player(p["name"], "추정"), "gs": g} for g, _, p in gs[:5]]
        basis = f"출처가 없어 지난 정규시즌({RV._prev(cur)}) 선발 출전 수 상위 5명 (프리시즌 선발은 쓰지 않음)"

    # 부상 선발의 대체 후보: 기사 + ESPN 뎁스 차트 같은 포지션 다음 순서
    repl = []
    st_norm = {norm(p["name"]) for p in starters}
    for p in starters:
        if not p["injury"]:
            continue
        cands = [{"name": r["candidate"], "basis": r["text"], "source": r["source"], "url": r["url"], "date": r["published"][:10]}
                 for r in art.get("replacements", []) if r.get("team") == tri and norm(r["for"]) == norm(p["name"])]
        if dc:
            for pos in POS:
                names_ = [a["name"] for a in dc["positions"][pos]]
                if names_ and norm(names_[0]) == norm(p["name"]):
                    nxt = next((n for n in names_[1:] if norm(n) not in st_norm and norm(n) not in injured), None)
                    if nxt:
                        cands.append({"name": nxt, "basis": f"ESPN 뎁스 차트 {pos.upper()} 다음 순서", "source": "ESPN 뎁스 차트",
                                      "url": dc["url"], "date": (dc["checked_utc"] or "")[:10]})
        repl.append({"for": p["name"], "status": p["injury"]["status"], "part": p["injury"]["part"], "candidates": cands})
    notes = [n for n in art.get("notes", []) if n.get("team") == tri and
             tip - datetime.fromisoformat(n["published"][:10] + "T00:00:00+00:00") <= timedelta(days=ARTICLE_DAYS)]

    # 유닛: 뎁스 차트 순서(가장 높은 순위) → 같은 줄이면 지난 시즌 경기당 출전 시간
    units = {"starters": [p["id"] for p in starters if p["id"]], "second": [], "third": []}
    others = []
    best: dict[int, int] = {}
    if dc:
        for pos in POS:
            for k, a in enumerate(dc["positions"][pos]):
                rp = find(a["name"])
                if rp:
                    best[rp["id"]] = min(best.get(rp["id"], 99), k)
    rest = [p for p in roster if p["id"] not in units["starters"]]
    rest.sort(key=lambda p: (best.get(p["id"], 99), -(prev_stats.get(p["id"]) or {}).get("min", 0)))
    if dc is None:
        # 뎁스 차트를 끝내 못 받음: 지난 시즌 경기당 출전 시간 순서로 채우고 '추정' 표시
        injured_ids = {find(i["name"])["id"] for i in inj if i["status"] == "결장" and find(i["name"])}
        healthy = [p for p in rest if p["id"] not in injured_ids]
        units["second"] = [p["id"] for p in healthy[:5]]
        units["third"] = [p["id"] for p in healthy[5:10]]
        others = [p["id"] for p in rest if p["id"] not in units["second"] + units["third"]]
        rest = []
    for p in rest:
        if best.get(p["id"], 99) <= 1 and len(units["second"]) < 5:
            units["second"].append(p["id"])
    for p in rest:
        if p["id"] in units["second"]:
            continue
        if len(units["second"]) < 5 and best.get(p["id"], 99) <= 2:
            units["second"].append(p["id"])
        elif len(units["third"]) < 5 and best.get(p["id"], 99) < 99:
            units["third"].append(p["id"])
        else:
            others.append(p["id"])
    info = {p["id"]: {"name": p["name"], "short": name_i(p["name"]), "pos": p["pos"],
                      "depth": best.get(p["id"]), "mpg": (prev_stats.get(p["id"]) or {}).get("min"),
                      "prev_team": (prev_stats.get(p["id"]) or {}).get("team")} for p in roster}
    for p in starters:   # 로스터에 없는 이름(출처 표기 차이)도 화면에 보이게
        if p["id"] is None:
            errors.append(f"{p['name']}: 로스터에서 찾지 못함")

    # 배지 (선발 + 세컨 + 서드)
    bdg, berr = {}, 0
    for pid in units["starters"] + units["second"] + units["third"]:
        try:
            b = badges({"id": pid}, tri, focus, cur, prev_stats)
            if b:
                bdg[pid] = b
        except Exception:
            berr += 1
    if berr:
        errors.append(f"배지: {berr}명 기록을 받지 못함")
    return {"team": tri, "level": level, "basis": basis, "starters": starters, "sources": sources, "old_sources": old,
            "dissent": dissent, "replacements": repl, "notes": notes, "units": units, "others": others, "players": info,
            "badges": bdg, "depth_url": dc["url"] if dc else None, "depth_status": depth_status,
            "units_basis": "estimate" if dc is None else "depth", "errors": errors}


def build(game: dict, focus: str, inj_by_team: dict[str, list], official: dict | None, on_recover=None) -> dict:
    cur = RV._season_of(datetime.now(timezone.utc))
    teams = [game["away"], game["home"]]
    try:
        roto = rotowire(game["home"], game["away"])
    except Exception:
        roto = None
    art = article_sources(game["game_id"])
    prev_stats = season_stats(RV._prev(cur))
    out = {"game_id": game["game_id"], "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "official": bool(official), "teams": {}, "article_file": {k: art.get(k) for k in ("written_utc", "written_by", "policy")},
           "season": cur, "prev_season": RV._prev(cur),
           "mpg_basis": f"{RV._prev(cur)} 정규시즌 경기당 출전 시간 (stats.nba.com)"}
    for tri in teams:
        out["teams"][tri] = team_lineup(tri, focus, game, cur, official, roto, art, inj_by_team.get(tri, []), prev_stats,
                                       on_recover)
    return out


class LineupService:
    """경기별로 백그라운드 계산. 팁오프 2시간 전부터는 2분마다 공식 선발 발표를 확인하고, 나오면 즉시 다시 만든다."""

    def __init__(self, game: dict, focus: str, injuries_fn):
        self.game, self.focus, self.injuries_fn = game, focus, injuries_fn
        self.data: dict | None = None
        self.official: dict | None = None
        self.busy = False
        self.error: str | None = None
        self.built_at = 0.0
        self.checked_official = 0.0
        self.fails = 0
        self.lock = threading.Lock()

    def _rebuild(self, reason: str) -> None:
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                d = build(self.game, self.focus, self.injuries_fn() or {}, self.official,
                          on_recover=lambda: self._rebuild("뎁스 차트 다시 받음"))
                d["reason"] = reason
                with self.lock:
                    self.data, self.error = d, None
                RV._write(os.path.join(CACHE, f"lineup_{self.game['game_id']}.json"), d)
                print(f"[선발] {reason}: " + " / ".join(f"{t} {v['level']}" for t, v in d["teams"].items()), flush=True)
            except Exception as exc:   # 에러 원문은 로그 파일에만
                RS.log_error("선발 예측", exc)
                self.fails += 1
                wait = RS.RETRY[min(self.fails, len(RS.RETRY)) - 1]
                self.error = f"선발 예측을 만들지 못했습니다 ({wait if wait < 60 else wait // 60}{'초' if wait < 60 else '분'} 뒤 다시 시도)"
                print(f"[선발] 계산 실패 ({type(exc).__name__}) → logs/fetch_errors.log", flush=True)
            else:
                self.fails = 0
            finally:
                self.built_at = time.time()
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def tick(self) -> None:
        now = time.time()
        retry = RS.RETRY[min(self.fails, len(RS.RETRY)) - 1] if self.fails else 0   # 30초 → 1분 → 5분
        if self.data is None and NH.is_live():
            # 닉스 경기 중(서버를 경기 도중 다시 켠 경우)엔 ESPN 뎁스 차트를 묻지 않는다 → 저장해 둔 마지막 계산을 쓰고, 없으면 경기 뒤에
            saved = RS._load(os.path.join(CACHE, f"lineup_{self.game['game_id']}.json"))
            if saved:
                with self.lock:
                    self.data = saved
                self.built_at = now
            return
        if self.data is None and not self.busy and now - self.built_at > retry:
            self._rebuild("처음 계산")
            return
        tip = datetime.fromisoformat(self.game["tipoff_utc"].replace("Z", "+00:00")).timestamp()
        if not self.official and tip - 7200 <= now <= tip + 3600 and now - self.checked_official > 120:
            self.checked_official = now
            off = official_starters(self.game["game_id"])
            if off:
                self.official = off
                self._rebuild("공식 선발 발표")
                return
        if self.data and now - self.built_at > 3600 and not NH.is_live():   # 기사·부상·로토와이어 반영: 1시간마다 (라이브 중엔 미룸)
            self._rebuild("정기 갱신")

    def expected_names(self) -> list[str]:
        d = self.data
        if not d:
            return []
        return [p["short"] for p in d["teams"].get(self.focus, {}).get("starters", [])]

    def snapshot(self) -> dict:
        with self.lock:
            if not self.data:
                return {"status": "error" if self.error else "loading", "error": self.error}
            return {"status": "ready", **self.data}
