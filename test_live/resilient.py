"""외부 데이터 요청 공통 처리 (뎁스 차트·부상·뉴스·일정·순위 …).

- 성공하면 결과를 저장해 둔다 (마지막으로 성공한 데이터).
- 실패하면 에러 원문은 logs/fetch_errors.log 에만 남기고, 저장해 둔 데이터를 돌려준다.
  저장본도 없으면 Unavailable(화면에 그대로 보여도 되는 짧은 한국어 문구)을 던진다.
- 실패한 요청은 백그라운드에서 30초 → 1분 → 5분 뒤 다시 시도하고, 그 뒤로도 5분마다 최대 1시간까지 시도한다.
  재시도 중에는 같은 요청이 와도 외부에 다시 묻지 않고 저장본을 돌려준다 (화면 요청이 느려지지 않게).
  재시도가 성공하면 저장하고 on_recover 를 불러 화면 데이터를 다시 만들게 한다.
- notices() 는 지금 실패 중인 데이터 목록(무엇이·몇 시 기준 데이터인지)을 돌려준다. 화면은 이걸로 작게 표시한다.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import nba_http as NH

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "logs", "fetch_errors.log")
RETRY = (30, 60, 300)          # 첫 재시도 간격들
RETRY_AFTER = 300              # 그 뒤로는 5분마다
RETRY_FOR = 3600               # 처음 실패부터 1시간 동안
LIVE_RETRY = 900               # 라이브 경기 중에는 재시도를 15분 간격으로 늦춘다 (NBA 서버로 가는 요청 줄이기)
KST = timezone(timedelta(hours=9))

# 저장 파일 이름 앞부분 → (화면 묶음, 화면 이름)
GROUPS = (("depth_", "depth", "뎁스 차트"), ("injuries", "injuries", "부상 정보"), ("news", "news", "뉴스"),
          ("schedule", "schedule", "일정"), ("standings", "standings", "순위"), ("event_", "injuries", "부상 정보"),
          ("league_totals", "other", "리그 기준값(stats.nba.com)"),
          ("league_misc", "other", "리그 실책 기준값(stats.nba.com)"), ("pg_", "other", "경기 기록"))


class Unavailable(Exception):
    """받지 못했고 저장본도 없음. str() 은 화면에 보여도 되는 문구."""


_lock = threading.Lock()
_state: dict[str, dict] = {}    # key → {label, group, path, fails, first_fail, next_utc, gave_up, thread}


def group_of(key: str) -> tuple[str, str]:
    for prefix, group, label in GROUPS:
        if key.startswith(prefix):
            return group, label
    return "other", "기록"


def log_line(label: str, key: str, msg: str) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{datetime.now(KST).isoformat(timespec='seconds')} [{label}{' ' + key if key else ''}] {msg}\n")


_quiet: dict[str, float] = {}


def log_error(label: str, exc: BaseException, key: str = "", note: str = "") -> None:
    """에러 원문은 여기에만 (화면에는 보이지 않는다).
    요청 관리자가 보내지 않은 요청(쉬는 중·연결 없음)은 실패가 아니므로 같은 데이터당 30분에 한 줄만 남긴다."""
    if isinstance(exc, NH.NotSent):
        k = f"{label}|{key}"
        if time.time() - _quiet.get(k, 0) < 1800:
            return
        _quiet[k] = time.time()
    tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    log_line(label, key, f"{note}{' · ' if note else ''}{tb}")


def _josa(word: str) -> str:
    word = word.split("(")[0].strip() or word   # '리그 기준값(stats.nba.com)' → '리그 기준값'
    last = ord(word[-1]) - 0xAC00
    return "을" if 0 <= last < 11172 and last % 28 else "를"


def friendly(label: str, exc: BaseException, key: str = "", then: str = "다음 갱신 때 다시 시도") -> str:
    """에러 원문은 로그 파일에 남기고, 화면에는 '○○을 받지 못했습니다 (…)' 만."""
    if isinstance(exc, Unavailable):
        return str(exc)
    log_error(label, exc, key)
    if isinstance(exc, NH.NotSent) and "닉스 경기 중" in str(exc):   # 라이브 중 미룬 ESPN 요청
        return f"{label}{_josa(label)} 닉스 경기가 끝난 뒤 받습니다"
    return f"{label}{_josa(label)} 받지 못했습니다 ({then})"


def _save(path: str, data) -> None:
    import rivals as RV   # NaN 정리·원자적 쓰기는 기존 함수를 그대로 쓴다
    os.makedirs(os.path.dirname(path), exist_ok=True)
    RV._write(path, data)


def _load(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def as_of(path: str) -> str | None:
    """저장본이 언제 받은 데이터인지 (UTC ISO)."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return None


def fetch(key: str, fn, path: str, label: str | None = None, on_recover=None):
    """fn() 결과를 path 에 저장하고 돌려준다. 실패하면 저장본을 돌려주고(없으면 Unavailable) 재시도를 예약한다."""
    group, glabel = group_of(key)
    label = label or glabel
    with _lock:
        st = _state.get(key)
        if st and st["thread"] and st["thread"].is_alive():
            if on_recover:
                st["callbacks"].append(on_recover)
            busy = True
        else:
            busy = False
    if busy:   # 재시도가 돌고 있으면 외부에 묻지 않고 저장본
        return _fallback(key, path, label)
    if NH.is_live():   # 라이브 중: 비긴급 갱신은 미루고 저장본 (저장본이 없을 때만 받는다)
        data = _load(path)
        if data is not None:
            _note_deferred(key, label)
            return data
    try:
        data = fn()
    except Exception as exc:
        log_error(label, exc, key)
        _schedule(key, fn, path, label, group, on_recover)
        return _fallback(key, path, label)
    _save(path, data)
    with _lock:
        _state.pop(key, None)
    return data


_deferred: dict[str, float] = {}


def _note_deferred(key: str, label: str) -> None:
    if time.time() - _deferred.get(key, 0) > 1800:   # 같은 데이터는 30분에 한 줄만
        log_line(label, key, "라이브 경기 중이라 갱신을 미루고 저장본 사용")
    _deferred[key] = time.time()


def deferred() -> list[str]:
    """최근 10분 안에 라이브 때문에 갱신을 미룬 데이터 키."""
    return [k for k, t in _deferred.items() if time.time() - t < 600]


def _fallback(key: str, path: str, label: str):
    data = _load(path)
    if data is None:
        raise Unavailable(f"{label}{_josa(label)} 받지 못했습니다 (자동으로 다시 시도 중)")
    return data


def _schedule(key, fn, path, label, group, on_recover) -> None:
    with _lock:
        st = _state.get(key)
        if st and st["thread"] and st["thread"].is_alive():
            return
        now = time.time()
        st = st or {"label": label, "group": group, "path": path, "fails": 0, "first_fail": now, "callbacks": []}
        st.update(fails=st["fails"] + 1, gave_up=False, thread=None)
        if on_recover:
            st["callbacks"].append(on_recover)
        _state[key] = st
        t = threading.Thread(target=_retry_loop, args=(key, fn, path, label), daemon=True, name=f"retry-{key}")
        st["thread"] = t
    t.start()


def _retry_loop(key, fn, path, label) -> None:
    st = _state[key]
    n = 0
    while True:
        wait = RETRY[n] if n < len(RETRY) else RETRY_AFTER
        if NH.is_live():   # 라이브 중엔 늦추고, 그 시간은 1시간 포기 기준에 넣지 않는다
            wait = max(wait, LIVE_RETRY)
            st["first_fail"] += wait
        if time.time() + wait - st["first_fail"] > RETRY_FOR and n >= len(RETRY):
            with _lock:
                st["gave_up"], st["next_utc"] = True, None
            log_line(label, key, "1시간 동안 받지 못해 재시도를 멈춤 (다음 정기 갱신 때 다시 시도)")
            return
        with _lock:
            st["next_utc"] = datetime.fromtimestamp(time.time() + wait, timezone.utc).isoformat(timespec="seconds")
            st["attempt"] = n + 1
        time.sleep(wait)
        try:
            data = fn()
        except Exception as exc:
            n += 1
            with _lock:
                st["fails"] += 1
            log_error(label, exc, key, f"재시도 {n}번째 실패")
            continue
        _save(path, data)
        with _lock:
            _state.pop(key, None)
            cbs = list(st["callbacks"])
        log_line(label, key, f"재시도 {n + 1}번째에 다시 받음")
        for cb in dict.fromkeys(cbs):
            try:
                cb()
            except Exception as exc:
                log_error(label, exc, key, "다시 받은 뒤 화면 갱신 실패")
        return


def failing(key: str) -> dict | None:
    """이 데이터가 지금 실패 중이면 {label, as_of, retrying, gave_up, next_utc}."""
    with _lock:
        st = _state.get(key)
        if not st:
            return None
        return {"key": key, "label": st["label"], "group": st["group"], "as_of": as_of(st["path"]),
                "retrying": bool(st["thread"] and st["thread"].is_alive()), "gave_up": st.get("gave_up", False),
                "attempt": st.get("attempt"), "next_utc": st.get("next_utc"), "fails": st["fails"]}


def notices() -> list[dict]:
    """지금 실패 중인 데이터 전부 (화면 표시용, 에러 원문 없음)."""
    with _lock:
        keys = list(_state)
    return [n for n in (failing(k) for k in keys) if n]
