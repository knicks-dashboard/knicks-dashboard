"""피리어드 시작 라인업 추론.

PBP 에는 쿼터 사이 교체가 빠지는 경우가 있어 출전 시간/+- 계산에 필요한
'피리어드 시작 시 코트 위 5명'을 따로 구한다.
"""
from __future__ import annotations

from .model import BLOCK, FOUL, REBOUND, SHOT, STEAL, SUB, TURNOVER, FREE_THROW, Event, GameMeta

Starters = dict[int, dict[str, list[int]]]  # period -> tricode -> [player_id x5]

_ACTIVE_KINDS = {SHOT, FREE_THROW, REBOUND, TURNOVER, STEAL, BLOCK, FOUL}


def infer_from_events(meta: GameMeta, events: list[Event]) -> Starters:
    """완료된 피리어드들의 시작 라인업을 이벤트만으로 추론한다.

    1쿼터는 박스스코어 선발. 이후 쿼터는 '그 쿼터에서 교체로 들어오기 전에
    먼저 등장한 선수'를 시작 멤버로 보고, 5명이 안 되면 직전 쿼터 종료 라인업으로 채운다.
    """
    result: Starters = {}
    periods = sorted({e.period for e in events})
    prev_end: dict[str, list[int]] = {}
    for period in periods:
        pev = [e for e in events if e.period == period]
        result[period] = {}
        for team in (meta.home, meta.away):
            if period == 1:
                start = [p.id for p in team.players if p.starter]
            else:
                start = _first_appearance(pev, team.tricode)
                if len(start) < 5:
                    for pid in prev_end.get(team.tricode, []):
                        if pid not in start and not _subbed_in_before_out(pev, pid):
                            start.append(pid)
                        if len(start) == 5:
                            break
                start = start[:5]
            result[period][team.tricode] = start
            prev_end[team.tricode] = _apply_subs(start, pev, team.tricode)
    return result


def from_current_oncourt(events: list[Event], period: int, tricode: str, oncourt: list[int]) -> list[int]:
    """현재 코트 위 선수(라이브 박스스코어)에서 이번 피리어드 교체를 거꾸로 되돌려 시작 라인업을 구한다."""
    lineup = list(oncourt)
    for e in reversed([e for e in events if e.period == period and e.kind == SUB and e.team == tricode]):
        if e.player_id in lineup:
            lineup.remove(e.player_id)
        if e.out_id is not None and e.out_id not in lineup:
            lineup.append(e.out_id)
    return lineup


def _first_appearance(pev: list[Event], tricode: str) -> list[int]:
    seen, starters = set(), []
    for e in pev:
        if e.team != tricode:
            continue
        if e.kind == SUB:
            if e.out_id is not None and e.out_id not in seen:
                starters.append(e.out_id)
                seen.add(e.out_id)
            if e.player_id is not None:
                seen.add(e.player_id)
            continue
        if e.kind in _ACTIVE_KINDS and e.player_id and not (e.kind == FOUL and not e.personal):
            for pid in (e.player_id, e.assist_id):
                if pid and pid not in seen:
                    starters.append(pid)
                    seen.add(pid)
    return starters


def _subbed_in_before_out(pev: list[Event], pid: int) -> bool:
    for e in pev:
        if e.kind != SUB:
            continue
        if e.player_id == pid:
            return True
        if e.out_id == pid:
            return False
    return False


def _apply_subs(start: list[int], pev: list[Event], tricode: str) -> list[int]:
    lineup = list(start)
    for e in pev:
        if e.kind == SUB and e.team == tricode:
            if e.out_id in lineup:
                lineup.remove(e.out_id)
            if e.player_id is not None and e.player_id not in lineup:
                lineup.append(e.player_id)
    return lineup
