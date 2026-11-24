"""외부 요청 관리자: NBA(cdn.nba.com · stats.nba.com) 와 ESPN 으로 가는 모든 요청이 여기를 지난다.

1) 끝난 경기 영구 저장: cdn 박스스코어·플레이바이플레이가 '경기 끝'이면 cache/final/ 에 저장하고, 그 뒤로는
   같은 주소를 요청해도 네트워크로 보내지 않고 저장본을 돌려준다 (호출하는 쪽 코드는 그대로).
   ESPN 경기 요약(summary)도 경기가 끝난 것이면 같은 방식. 예전에 replay_data/ 에 받아 둔 파일도 저장본으로 쓴다.
2) 사이트별 분당 요청 수 제한 (LIMITS). 넘으면 자리가 날 때까지 기다린다 (45초 넘게 기다려야 하면 보내지 않음).
3) 차단(403·429)을 받으면 그 사이트로는 30분 → (다시 차단이면) 1시간 → 2시간 동안 요청하지 않는다.
   시간 초과·연결 실패·5xx 는 짧게 30초 → 1분 → 2분 → 최대 5분. 한 번 성공하면 풀린다.
   쉬는 상태는 cache/net_state.json 에 남겨 서버를 다시 켜도 이어진다 (다시 켜자마자 차단된 사이트를 두드리지 않게).
4) 인터넷 연결 확인·잠 깸 감지: 연결이 없거나 Mac 이 막 잠에서 깼으면 보내지 않고, 연결이 확인된 뒤 재개한다.
   (잠 깸: 벽시계는 흘렀는데 monotonic 시계는 멈춰 있던 차이로 안다 — macOS 의 monotonic 은 잠자는 동안 멈춘다)
5) 요청 수 집계: logs/requests/<날짜>.json (한국 날짜, 사이트별 보냄·성공·차단·실패·안 보냄·저장본 사용).
6) cdn.nba.com 정책 (집 IP 가 Akamai 에 막힘, 2026-11-03): 처음 48시간은 완전 정지(확인 요청도 없음) →
   그 뒤 하루 한 번 scoreboard 하나로만 풀렸는지 확인 → 풀리면 '라이브 중인 닉스 경기' 요청만 보낸다.
   끝난 경기는 1) 의 저장본만 쓴다. 다시 사용하다 또 차단되면 하루 한 번 확인 단계로 돌아간다.
7) 닉스 라이브 경기 중에는 ESPN 요청 중 라이브 추적과 부상 정보만 보낸다 (리그 데스크·뎁스 차트 등은 경기 뒤로).
   용도는 요청 헤더 X-Knicks-Purpose 로 알린다 (live · injuries · probe). 이 헤더는 실제로 보내지 않는다.
8) 데모 모드 (app.py --demo, 배포용): 어떤 사이트로도 요청을 보내지 않고, 연결 확인도 하지 않는다.

보내지 않은 요청은 NotSent(BackedOff / Offline) 를 던진다. 호출하는 쪽은 실패와 똑같이 저장본·대체 소스로 처리한다.
"""
from __future__ import annotations

import json
import os
import random
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
FINAL_DIR = os.path.join(HERE, "cache", "final")
STATE_FILE = os.path.join(HERE, "cache", "net_state.json")
COUNT_DIR = os.path.join(HERE, "logs", "requests")
REPLAY_DATA = os.path.join(HERE, "replay_data")
DEMO_DIR = os.path.join(HERE, "demo")          # 데모 모드 데이터 (make_demo.py 가 만든다)
DEMO = False                                   # 데모 모드: 외부로 아무 요청도 보내지 않는다 (set_demo)
KST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}
STATS_EXTRA = {"x-nba-stats-origin": "stats", "x-nba-stats-token": "true"}
ESPN_HOST = "site.api.espn.com"
ESPN_CORE_HOST = "sports.core.api.espn.com"
ESPN_HEADERS = {k: HEADERS[k] for k in ("User-Agent", "Accept", "Accept-Language", "Connection")}   # nba.com Referer·Origin 은 빼고
TIMEOUT = (5, 15)

# 사이트별 분당 최대 요청 수. 라이브 폴링(cdn 15초마다 3건 = 분당 12건 · ESPN 10초마다 1건 = 분당 6건)이 들어가고,
# 배경 작업은 그 안에서 줄을 선다. cdn 은 다시 열려도 라이브 닉스 경기만 받으므로 사실상 라이브 몫.
LIMITS = {"cdn.nba.com": 14, "stats.nba.com": 10, ESPN_HOST: 20, ESPN_CORE_HOST: 20}
HOSTS = tuple(LIMITS)
NAMES = {"cdn.nba.com": "cdn.nba.com", "stats.nba.com": "stats.nba.com", ESPN_HOST: "ESPN", ESPN_CORE_HOST: "ESPN 배당·예측"}
BLOCK_PAUSE = (1800, 3600, 7200)      # 403·429: 30분 → 1시간 → 2시간 (연속 차단일 때)
ERROR_BACKOFF = (30, 60, 120, 300)    # 시간 초과·연결 실패·5xx
# ESPN 은 라이브 대체 경로라 길게 쉬면 경기를 놓친다: 429 는 20초 → 30초 (라이브 갱신 간격과 맞춤) → 1분 → 5분
ESPN_BLOCK_PAUSE = (20, 30, 60, 300)
ESPN_ERROR_BACKOFF = (10, 20, 30, 60)
JITTER = 0.15
MAX_QUEUE_WAIT = 45                   # 분당 한도로 이보다 오래 기다려야 하면 보내지 않는다
BLOCK_CODES = {403, 429}
ERROR_CODES = {500, 502, 503, 504}
WAKE_GRACE = 20                       # 잠에서 깬 뒤 연결 확인이 되어도 이만큼 더 기다린다 (Wi-Fi 재연결 여유)


class NotSent(RuntimeError):
    """요청을 보내지 않았다 (쉬는 중 · 연결 없음). 실패와 같게 처리하되, 서버에 부담을 준 것은 아니다."""


class BackedOff(NotSent):
    pass


class Offline(NotSent):
    pass


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None


# ---------------------------------------------------------------- 요청 수 집계

class Counter:
    """여러 프로세스(서버·테스트 서버·명령줄)가 같은 날짜 파일에 더해 쓴다: 메모리에는 아직 안 쓴 증가분만 두고,
    저장할 때 파일을 읽어 더한 뒤 쓴다 (덮어쓰지 않는다)."""
    KEYS = ("sent", "ok", "blocked", "failed", "not_sent", "cache_hit", "waited")

    def __init__(self):
        self.lock = threading.Lock()
        self.day = None
        self.pending: dict = {}
        self.saved_at = 0.0

    @staticmethod
    def _blank() -> dict:
        return {k: 0 for k in Counter.KEYS} | {"by_hour": {}, "by_kind": {}}

    def _path(self, day: str) -> str:
        return os.path.join(COUNT_DIR, f"{day}.json")

    def _read(self, day: str) -> dict:
        try:
            with open(self._path(day)) as f:
                return json.load(f)["hosts"]
        except (OSError, ValueError, KeyError):
            return {}

    @staticmethod
    def _merge(into: dict, add: dict) -> None:
        for h, v in add.items():
            t = into.setdefault(h, Counter._blank())
            for k, n in v.items():
                if isinstance(n, dict):
                    for kk, nn in n.items():
                        t.setdefault(k, {})[kk] = t.get(k, {}).get(kk, 0) + nn
                else:
                    t[k] = t.get(k, 0) + n

    def add(self, host: str, key: str, n: int = 1, tag: str | None = None) -> None:
        with self.lock:
            day = datetime.now(KST).date().isoformat()
            if day != self.day:
                if self.day:
                    self._flush()
                self.day = day
            h = self.pending.setdefault(host, self._blank())
            h[key] = h.get(key, 0) + n
            if key == "sent":
                hr = datetime.now(KST).strftime("%H")
                h["by_hour"][hr] = h["by_hour"].get(hr, 0) + n
                if tag:
                    h["by_kind"][tag] = h["by_kind"].get(tag, 0) + n
            if time.time() - self.saved_at > 30:
                self._flush()

    def _flush(self) -> None:
        self.saved_at = time.time()
        if not self.pending or not self.day:
            return
        try:
            os.makedirs(COUNT_DIR, exist_ok=True)
            data = self._read(self.day)
            self._merge(data, self.pending)
            tmp = os.path.join(COUNT_DIR, f".{self.day}.{os.getpid()}.tmp")
            with open(tmp, "w") as f:
                json.dump({"day_kst": self.day, "saved_kst": datetime.now(KST).isoformat(timespec="seconds"),
                           "hosts": data}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path(self.day))
            self.pending = {}
        except OSError:
            pass

    def today(self) -> dict:
        with self.lock:
            day = datetime.now(KST).date().isoformat()
            data = self._read(day)
            if self.day == day:
                self._merge(data, self.pending)
            return {h: {k: v.get(k, 0) for k in self.KEYS} for h, v in data.items()}


COUNT = Counter()


def _kind(url: str) -> str:
    """집계용 요청 종류 (예: cdn boxscore, stats leaguedashplayerstats, ESPN summary)."""
    p = urlparse(url).path.rstrip("/")
    if "/liveData/" in p:
        return p.split("/liveData/")[1].split("/")[0]
    return p.split("/")[-1][:40] or "?"


# ---------------------------------------------------------------- 사이트별 상태

class _Gate:
    def __init__(self, host: str):
        self.host = host
        self.fails = 0             # 시간 초과·연결 실패 연속 횟수
        self.blocks = 0            # 차단 연속 횟수
        self.next_at = 0.0
        self.reason: str | None = None     # "block" | "error"
        self.last_error: str | None = None
        self.last_ok_utc: str | None = None
        self.last_fail_utc: str | None = None
        self.sent = deque()        # 최근 60초 보낸 시각

    def ok(self) -> None:
        changed = self.fails or self.blocks or self.next_at
        self.fails = self.blocks = 0
        self.next_at, self.reason = 0.0, None
        self.last_ok_utc = _iso(time.time())
        if changed:
            _save_state()

    def fail(self, why: str, block: bool) -> None:
        self.last_error = why
        self.last_fail_utc = _iso(time.time())
        if time.time() < self.next_at:   # 같은 순간에 나간 다른 요청의 실패: 이미 쉬는 중이므로 한 번만 센다
            return
        espn = self.host in (ESPN_HOST, ESPN_CORE_HOST)
        if block:
            self.blocks += 1
            seq = ESPN_BLOCK_PAUSE if espn else BLOCK_PAUSE
            base = seq[min(self.blocks, len(seq)) - 1]
            self.reason = "block"
        else:
            self.fails += 1
            seq = ESPN_ERROR_BACKOFF if espn else ERROR_BACKOFF
            base = seq[min(self.fails, len(seq)) - 1]
            self.reason = "error"
        self.next_at = time.time() + base * (1 + random.uniform(0, JITTER))
        _save_state()

    def status(self) -> dict:
        wait = self.next_at - time.time()
        return {"host": self.host, "name": NAMES[self.host], "paused": wait > 0, "reason": self.reason if wait > 0 else None,
                "resume_utc": _iso(self.next_at) if wait > 0 else None, "wait_sec": max(0, round(wait)),
                "fails": self.fails, "blocks": self.blocks, "last_error": self.last_error,
                "last_ok_utc": self.last_ok_utc, "last_fail_utc": self.last_fail_utc,
                "per_min_limit": LIMITS[self.host], "sent_last_min": len(self.sent)}


_lock = threading.Lock()
GATES = {h: _Gate(h) for h in HOSTS}


def _save_state() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        data = {h: {"next_at": g.next_at, "blocks": g.blocks, "fails": g.fails, "reason": g.reason, "last_error": g.last_error,
                    "last_ok_utc": g.last_ok_utc, "last_fail_utc": g.last_fail_utc} for h, g in GATES.items()}
        data["cdn_policy"] = CDN_POLICY.dump()
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except OSError:
        pass


def _load_state() -> None:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    for h, v in data.items():
        g = GATES.get(h)
        if g:
            g.next_at, g.blocks, g.fails = v.get("next_at", 0.0), v.get("blocks", 0), v.get("fails", 0)
            g.reason, g.last_error = v.get("reason"), v.get("last_error")
            g.last_ok_utc, g.last_fail_utc = v.get("last_ok_utc"), v.get("last_fail_utc")
    if data.get("cdn_policy"):
        CDN_POLICY.load(data["cdn_policy"])
    else:   # 정책이 처음 생긴 때 (2026-11-03): 지금부터 48시간 완전 정지
        CDN_POLICY.start_stop()
        _save_state()


# ---------------------------------------------------------------- cdn.nba.com 정책

CDN = "cdn.nba.com"
CDN_STOP_HOURS = 48                    # 완전 정지 (확인 요청도 없음)
CDN_PROBE_EVERY = 24 * 3600            # 그 뒤 하루 한 번 확인
CDN_PROBE_URL = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
PURPOSE = "X-Knicks-Purpose"           # 요청 용도 (live · injuries · probe) — 이 관리자 안에서만 읽고 보내지 않는다


class _CdnPolicy:
    """stopped(완전 정지) → probing(하루 한 번 scoreboard 로 확인) → live_only(라이브 닉스 경기만)."""

    def __init__(self):
        self.stop_until = 0.0
        self.next_probe = 0.0
        self.reopened = False
        self.last_probe: dict | None = None    # {"utc", "result"}

    def start_stop(self, hours: float = CDN_STOP_HOURS) -> None:
        self.stop_until = time.time() + hours * 3600
        self.next_probe, self.reopened = self.stop_until, False

    def dump(self) -> dict:
        return {"stop_until": self.stop_until, "next_probe": self.next_probe, "reopened": self.reopened,
                "last_probe": self.last_probe}

    def load(self, d: dict) -> None:
        self.stop_until, self.next_probe = d.get("stop_until", 0.0), d.get("next_probe", 0.0)
        self.reopened, self.last_probe = bool(d.get("reopened")), d.get("last_probe")

    def mode(self) -> str:
        if time.time() < self.stop_until:
            return "stopped"
        return "live_only" if self.reopened else "probing"

    def refuse(self, purpose: str | None) -> str | None:
        """보내면 안 되는 이유 (보내도 되면 None)."""
        m = self.mode()
        if m == "stopped":
            return f"집 IP 차단으로 완전 정지 중 ({_kst_label(self.stop_until)}에 첫 확인)"
        if m == "probing":
            return None if purpose == "probe" else f"차단 풀렸는지 하루 한 번만 확인 ({_kst_label(self.next_probe)} 확인 예정)"
        return None if purpose in ("live", "probe") else "라이브 중인 닉스 경기만 받음 (끝난 경기는 저장본)"

    def blocked_again(self) -> None:
        """다시 쓰다가 또 차단: 하루 한 번 확인 단계로 돌아간다."""
        if self.reopened:
            self.reopened = False
            self.next_probe = time.time() + CDN_PROBE_EVERY
            print(f"[요청 관리자] cdn.nba.com 다시 차단 → {_kst_label(self.next_probe)}에 다시 확인", flush=True)

    def status(self) -> dict:
        m = self.mode()
        return {"mode": m, "stop_until_utc": _iso(self.stop_until) if m == "stopped" else None,
                "check_utc": _iso(self.stop_until if m == "stopped" else self.next_probe) if m != "live_only" else None,
                "last_probe": self.last_probe}


CDN_POLICY = _CdnPolicy()


def _kst_label(t: float) -> str:
    d = datetime.fromtimestamp(t, KST)
    return f"{d.month}월 {d.day}일 {d.hour}시"


def _cdn_probe_due() -> None:
    """net-watch 스레드에서 10초마다: 확인할 때가 되면 scoreboard 하나만 보낸다.
    서버·테스트 서버 등 여러 프로세스가 있어도 한 번만 보내도록, 보내기 전에 파일의 예약 시각을 먼저 미룬다."""
    if CDN_POLICY.mode() != "probing" or time.time() < CDN_POLICY.next_probe or NET.ready():
        return
    try:
        with open(STATE_FILE) as f:
            on_disk = json.load(f).get("cdn_policy") or {}
        if on_disk.get("next_probe", 0) > time.time() or on_disk.get("reopened"):   # 다른 프로세스가 이미 확인함
            CDN_POLICY.load(on_disk)
            return
    except (OSError, ValueError):
        pass
    CDN_POLICY.next_probe = time.time() + CDN_PROBE_EVERY
    _save_state()
    try:
        r = SESSION.get(CDN_PROBE_URL, headers={PURPOSE: "probe"})
        ok = r.status_code == 200 and "scoreboard" in r.json()
        result = "HTTP 200 (풀림)" if ok else f"HTTP {r.status_code}"
    except Exception as exc:
        ok, result = False, f"{type(exc).__name__}: {str(exc)[:80]}"
    CDN_POLICY.last_probe = {"utc": _iso(time.time()), "result": result}
    if ok:
        CDN_POLICY.reopened = True
        print("[요청 관리자] cdn.nba.com 차단 풀림 → 라이브 닉스 경기만 다시 사용", flush=True)
    else:
        print(f"[요청 관리자] cdn.nba.com 아직 막힘 ({result}) → {_kst_label(CDN_POLICY.next_probe)}에 다시 확인", flush=True)
    _save_state()


# ---------------------------------------------------------------- 인터넷 연결·잠 깸

class _Net:
    def __init__(self):
        self.online: bool | None = None
        self.checked = 0.0
        self.woke_at: float | None = None
        self.hold_until = 0.0          # 잠 깬 직후 여유
        self.last_wall, self.last_mono = time.time(), time.monotonic()

    def probe(self) -> bool:
        """NBA 가 아닌 곳(공용 DNS 서버)에 TCP 연결만 해 본다 — NBA 요청 수에 들어가지 않는다."""
        for host in ("1.1.1.1", "8.8.8.8"):
            try:
                socket.create_connection((host, 443), timeout=3).close()
                return True
            except OSError:
                continue
        return False

    def tick(self) -> None:
        wall, mono = time.time(), time.monotonic()
        slept = (wall - self.last_wall) - (mono - self.last_mono)
        self.last_wall, self.last_mono = wall, mono
        if slept > 30:   # 벽시계만 흘렀다 = 잠자고 있었다
            self.woke_at = wall
            self.online = None
            print(f"[요청 관리자] Mac 이 잠에서 깸 ({int(slept)}초 잠) → 연결 확인 후 재개", flush=True)
        every = 15 if self.online is not True else 60
        if self.online is None or wall - self.checked > every:
            was = self.online
            self.online, self.checked = self.probe(), wall
            if self.online and (was is not True):
                self.hold_until = wall + (WAKE_GRACE if self.woke_at and wall - self.woke_at < 120 else 0)
                if was is False or self.woke_at:
                    print("[요청 관리자] 인터넷 연결 확인 → 요청 재개", flush=True)
            elif not self.online and was is not False:
                print("[요청 관리자] 인터넷 연결 없음 → 연결될 때까지 요청하지 않음", flush=True)

    def ready(self) -> str | None:
        """보내도 되면 None, 아니면 이유."""
        if self.online is None:
            self.tick()
        if not self.online:
            return "인터넷 연결 없음 (연결되면 자동 재개)"
        if time.time() < self.hold_until:
            return "Mac 이 잠에서 막 깸 (연결 안정 대기)"
        return None

    def status(self) -> dict:
        return {"online": self.online, "checked_utc": _iso(self.checked), "woke_utc": _iso(self.woke_at),
                "holding": time.time() < self.hold_until}


NET = _Net()


def _net_loop() -> None:
    while True:
        time.sleep(10)   # 첫 확인도 10초 뒤 (그 사이 데모 모드가 켜질 수 있게. 그 전 요청은 NET.ready() 가 직접 확인한다)
        if DEMO:
            return
        try:
            NET.tick()
            _cdn_probe_due()
        except Exception:
            pass


# ---------------------------------------------------------------- 끝난 경기 저장본

_CDN_GAME = re.compile(r"/liveData/(boxscore|playbyplay)/\1_(\d{10})\.json$")


def _final_path(url: str, params: dict | None) -> str | None:
    u = urlparse(url)
    if u.hostname == "cdn.nba.com":
        m = _CDN_GAME.search(u.path)
        return os.path.join(FINAL_DIR, "cdn", f"{m.group(1)}_{m.group(2)}.json") if m else None
    if u.hostname == ESPN_HOST and u.path.endswith("/summary") and params and params.get("event"):
        return os.path.join(FINAL_DIR, "espn", f"summary_{params['event']}.json")
    return None


def _is_final(url: str, body: dict) -> bool:
    u = urlparse(url)
    if u.hostname == "cdn.nba.com":
        g = body.get("game") or {}
        if "/boxscore/" in u.path:
            return g.get("gameStatus") == 3
        return any(a.get("actionType") == "game" and a.get("subType") == "end" for a in g.get("actions") or [])
    if u.hostname == ESPN_HOST:
        try:
            return bool(body["header"]["competitions"][0]["status"]["type"]["completed"])
        except (KeyError, IndexError, TypeError):
            return False
    return False


def _seed_from_replay(url: str) -> dict | None:
    """예전에 replay_data/<게임ID>/ 에 받아 둔 끝난 경기 파일이 있으면 그걸 저장본으로 쓴다."""
    m = _CDN_GAME.search(urlparse(url).path)
    if not m:
        return None
    try:
        with open(os.path.join(REPLAY_DATA, m.group(2), f"live_{m.group(1)}.json")) as f:
            game = json.load(f)
    except (OSError, ValueError):
        return None
    body = {"game": game}
    return body if _is_final(url, body) else None


def _cached_response(url: str, body_text: str) -> requests.Response:
    r = requests.Response()
    r.status_code, r._content, r.url, r.encoding = 200, body_text.encode(), url, "utf-8"
    r.headers["Content-Type"] = "application/json"
    r.headers["X-From-Final-Cache"] = "1"
    return r


def final_lookup(url: str, params: dict | None = None) -> str | None:
    path = _final_path(url, params)
    if not path:
        return None
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        pass
    seed = _seed_from_replay(url)
    if seed is not None:
        _write(path, json.dumps(seed))
        return json.dumps(seed)
    return None


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 세션

class NBASession(requests.Session):
    """관리 대상 사이트로 가는 요청에 저장본·연결 확인·분당 한도·쉬기·집계를 붙인다. 다른 사이트는 그대로 통과."""

    def request(self, method, url, **kw):
        host = urlparse(url).hostname or ""
        gate = GATES.get(host)
        if DEMO:   # 데모 모드: 어떤 사이트로도 보내지 않는다 (호출하는 쪽은 실패와 똑같이 저장본으로 처리)
            if gate:
                COUNT.add(host, "not_sent")
            raise Offline(f"{NAMES.get(host, host)} 요청 안 보냄: 데모 모드")
        if gate is None:
            return super().request(method, url, **kw)
        params = kw.get("params")
        pd = dict(params) if isinstance(params, (dict, list)) and params else None
        headers = {k: v for k, v in (kw.pop("headers", None) or {}).items() if k.lower() not in ("host",)}
        purpose = headers.pop(PURPOSE, None)
        hit = final_lookup(url, pd) if method.upper() == "GET" else None
        if hit is not None:
            COUNT.add(host, "cache_hit")
            return _cached_response(url, hit)
        if host == CDN:
            why = CDN_POLICY.refuse(purpose)
            if why:
                COUNT.add(host, "not_sent")
                raise BackedOff(f"cdn.nba.com 요청 안 보냄: {why}")
        elif host in (ESPN_HOST, ESPN_CORE_HOST) and purpose not in ("live", "injuries") and is_live():
            COUNT.add(host, "not_sent")
            raise BackedOff(f"{NAMES[host]} 요청 미룸: 닉스 경기 중 (경기 끝나면 재개)")
        why = NET.ready()
        if why:
            COUNT.add(host, "not_sent")
            raise Offline(f"{NAMES[host]} 요청 안 보냄: {why}")
        self._admit(gate)
        headers.update(ESPN_HEADERS if host in (ESPN_HOST, ESPN_CORE_HOST) else HEADERS)
        if host == "stats.nba.com":
            headers.update(STATS_EXTRA)
        kw["timeout"] = TIMEOUT
        # 봇 차단 시스템이 붙인 쿠키가 쌓이면 그 연결만 403 으로 막힌 적이 있어, 연결은 재사용하되 쿠키는 매번 비운다
        self.cookies.clear()
        COUNT.add(host, "sent", tag=_kind(url))
        try:
            r = super().request(method, url, headers=headers, **kw)
        except (requests.Timeout, requests.ConnectionError) as exc:
            COUNT.add(host, "failed")
            with _lock:
                gate.fail(type(exc).__name__, block=False)
            if isinstance(exc, requests.ConnectionError) and not isinstance(exc, requests.Timeout):
                NET.online = None   # 연결 자체가 끊겼을 수 있다 → 다음 요청 전에 다시 확인
            raise
        with _lock:
            if r.status_code in BLOCK_CODES:
                COUNT.add(host, "blocked")
                gate.fail(f"HTTP {r.status_code}" + (" (접근 차단)" if r.status_code == 403 else " (요청 과다)"), block=True)
                if host == CDN and purpose != "probe":
                    CDN_POLICY.blocked_again()
                    _save_state()
            elif r.status_code in ERROR_CODES:
                COUNT.add(host, "failed")
                gate.fail(f"HTTP {r.status_code}", block=False)
            elif r.status_code < 400:
                COUNT.add(host, "ok")
                gate.ok()
        if r.status_code == 200:
            path = _final_path(url, pd)
            if path:
                try:
                    if _is_final(url, r.json()):
                        _write(path, r.text)
                except ValueError:
                    pass
        return r

    def _admit(self, gate: _Gate) -> None:
        """쉬는 중이면 안 보냄. 분당 한도가 찼으면 자리가 날 때까지 기다림 (너무 오래면 안 보냄)."""
        waited = False
        while True:
            with _lock:
                now = time.time()
                if now < gate.next_at:
                    COUNT.add(gate.host, "not_sent")
                    left = int(gate.next_at - now)
                    what = "차단돼 쉬는 중" if gate.reason == "block" else "응답이 없어 쉬는 중"
                    raise BackedOff(f"{NAMES[gate.host]} {what} ({_left_ko(left)} 뒤 다시 시도 · 마지막 오류 {gate.last_error})")
                while gate.sent and now - gate.sent[0] > 60:
                    gate.sent.popleft()
                if len(gate.sent) < LIMITS[gate.host]:
                    gate.sent.append(now)
                    if waited:
                        COUNT.add(gate.host, "waited")
                    return
                wait = 60 - (now - gate.sent[0]) + 0.05
            if wait > MAX_QUEUE_WAIT:
                COUNT.add(gate.host, "not_sent")
                raise BackedOff(f"{NAMES[gate.host]} 분당 요청 한도({LIMITS[gate.host]}건) 초과")
            waited = True
            time.sleep(wait)


def _left_ko(sec: int) -> str:
    return f"{sec // 60}분" if sec >= 120 else f"{sec}초"


SESSION = NBASession()
_installed = False


def install() -> None:
    """nba_api 의 stats·live 클라이언트가 이 세션을 쓰게 하고, 연결 감시를 시작한다 (여러 번 불러도 한 번만)."""
    global _installed
    if _installed:
        return
    from nba_api.live.nba.library.http import NBALiveHTTP
    from nba_api.stats.library.http import NBAStatsHTTP
    NBAStatsHTTP.set_session(SESSION)
    NBALiveHTTP.set_session(SESSION)
    _load_state()
    threading.Thread(target=_net_loop, daemon=True, name="net-watch").start()
    _installed = True


def get_json(url: str, extra_headers: dict | None = None, params: dict | None = None, purpose: str | None = None) -> dict:
    headers = dict(extra_headers or {})
    if purpose:
        headers[PURPOSE] = purpose
    r = SESSION.get(url, headers=headers, params=params)
    if r.status_code != 200:
        host = urlparse(url).hostname
        raise RuntimeError(f"{NAMES.get(host, host)} HTTP {r.status_code}" + (" (접근 차단)" if r.status_code == 403 else ""))
    return r.json()


def open_now(host: str) -> bool:
    """지금 이 사이트로 요청을 보낼 수 있나 (쉬는 중 아님, 연결 있음)."""
    with _lock:
        return time.time() >= GATES[host].next_at and NET.online is not False


def cdn_usable_for_live() -> bool:
    """라이브 닉스 경기를 cdn.nba.com 에서 받아도 되나 (정책이 열려 있고 쉬는 중이 아님)."""
    return CDN_POLICY.mode() == "live_only" and open_now(CDN)


_live_every: dict[str, tuple[float, str | None]] = {}   # 라이브 추적이 쓰는 사이트 → (갱신 간격 초, 늘린 이유)


def set_live_every(host: str | None, sec: float | None = None, why: str | None = None) -> None:
    """라이브 추적이 지금 어느 사이트를 몇 초 간격으로 받는지 (상단 상태 줄 표시용). host=None 이면 지움."""
    _live_every.clear()
    if host:
        _live_every[host] = (sec, why)


def next_try_in(hosts=HOSTS) -> float:
    """지금 쉬는 사이트들 중 가장 빨리 풀리는 때까지 남은 초 (모두 열려 있으면 0)."""
    with _lock:
        waits = [GATES[h].next_at - time.time() for h in hosts]
    return max(0.0, min(waits))


# ---------------------------------------------------------------- 대체 소스 사용 표시

_fallback: dict[str, tuple[str, float]] = {}   # 막힌 사이트 → (대신 쓰는 소스, 마지막으로 쓴 시각)


def note_fallback(host: str, by: str) -> None:
    _fallback[host] = (by, time.time())


def status() -> dict:
    """화면 상단 표시용: 사이트별 정상 / 쉬는 중(○분 뒤) / 대체 소스 사용 중 + 연결 상태 + 오늘 요청 수."""
    today = COUNT.today()
    with _lock:
        out = {}
        for h, g in GATES.items():
            st = g.status()
            fb = _fallback.get(h)
            st["fallback"] = fb[0] if fb and time.time() - fb[1] < 600 else None
            st["today"] = today.get(h, {})
            le = _live_every.get(h) if is_live() else None
            st["live_every"], st["live_slow_why"] = (le[0], le[1]) if le else (None, None)
            out[h] = st
        out[CDN]["policy"] = CDN_POLICY.status()
    return {"hosts": out, "net": NET.status(), "live": is_live()}


# ---------------------------------------------------------------- 라이브 중 비긴급 요청 미루기

_live: dict[str, float] = {}     # 진행 중인 라이브 경기 → 마지막으로 '진행 중'을 확인한 시각


def live_on(game_id: str) -> None:
    _live[game_id] = time.time()


def live_off(game_id: str) -> None:
    _live.pop(game_id, None)


_busy_fn = None


def set_demo() -> None:
    """데모 모드 켜기 (app.py --demo). 이 뒤로는 외부 요청이 하나도 나가지 않는다."""
    global DEMO
    DEMO = True


def set_busy_fn(fn) -> None:
    """앱이 '닉스 경기 시간인가'를 알려 주는 함수를 등록한다 (팁오프 1시간 전 ~ 경기 끝)."""
    global _busy_fn
    _busy_fn = fn


def knicks_busy() -> bool:
    """닉스 경기 시간(또는 라이브 추적 중)이면 True — 리그 전체 데이터 같은 무거운 갱신은 이때 하지 않는다."""
    if is_live():
        return True
    try:
        return bool(_busy_fn and _busy_fn())
    except Exception:
        return False


DAY_START_KST = 15   # '하루'의 경계: 한국 시간 15시 (미국 밤 경기가 모두 끝난 뒤, 다음 경기 전)


def desk_day() -> str:
    """리그 전체 데이터를 하루 한 번 받을 때의 '오늘' (한국 시간 15시에 바뀐다)."""
    return (datetime.now(KST) - timedelta(hours=DAY_START_KST)).date().isoformat()


def is_live() -> bool:
    """라이브 경기가 진행 중인가 (5분 넘게 확인이 없으면 끝난 것으로 본다)."""
    now = time.time()
    for g, t in list(_live.items()):
        if now - t > 300:
            _live.pop(g, None)
    return bool(_live)


import atexit  # noqa: E402
atexit.register(lambda: COUNT._flush())   # 끝날 때 남은 증가분 저장
