"""소스(리플레이/라이브)와 무관한 공통 데이터 형식.

모든 어댑터는 원본 데이터를 GameMeta + list[Event] 로 바꿔야 한다.
state.py 는 이 형식만 알고, 데이터가 어디서 왔는지는 모른다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

REGULATION_PERIODS = 4
PERIOD_SEC = 12 * 60
OT_SEC = 5 * 60

# Event.kind 값
PERIOD_START = "period_start"
PERIOD_END = "period_end"
SHOT = "shot"              # 야투 (made, value=2|3, assist_id)
FREE_THROW = "free_throw"  # made
REBOUND = "rebound"        # offensive, team_rebound
TURNOVER = "turnover"
STEAL = "steal"
BLOCK = "block"
FOUL = "foul"              # personal(개인 파울 집계 여부)
SUB = "substitution"       # player_id=들어오는 선수, out_id=나가는 선수
TIMEOUT = "timeout"
JUMP_BALL = "jump_ball"
VIOLATION = "violation"
OTHER = "other"


@dataclass
class Player:
    id: int
    name: str          # 표시용 (예: "Jalen Brunson")
    family_name: str
    name_i: str        # 예: "J. Brunson"
    jersey: str = ""
    starter: bool = False


@dataclass
class Team:
    id: int
    tricode: str
    city: str
    name: str
    players: list[Player] = field(default_factory=list)

    def player_by_name(self, name: str) -> int | None:
        """PBP 설명에 나온 이름(보통 성)으로 선수 ID를 찾는다."""
        name = name.strip()
        for key in ("family_name", "name_i", "name"):
            hits = [p for p in self.players if getattr(p, key) == name]
            if len(hits) == 1:
                return hits[0].id
        hits = [p for p in self.players if name.endswith(p.family_name) or p.family_name.startswith(name)]
        return hits[0].id if len(hits) == 1 else None


@dataclass
class GameMeta:
    game_id: str
    title: str
    home: Team
    away: Team

    def team(self, tricode: str) -> Team:
        return self.home if tricode == self.home.tricode else self.away

    def other(self, tricode: str) -> str:
        return self.away.tricode if tricode == self.home.tricode else self.home.tricode

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    seq: int                 # 원본 순서 (소스 안에서 증가)
    kind: str
    period: int
    clock: float             # 해당 피리어드 남은 시간(초)
    team: str | None = None  # tricode
    player_id: int | None = None
    made: bool | None = None
    value: int = 0           # 득점 시 점수
    assist_id: int | None = None
    out_id: int | None = None
    offensive: bool | None = None
    team_rebound: bool = False
    personal: bool = True
    score_home: int | None = None
    score_away: int | None = None
    distance: float | None = None
    x: float | None = None
    y: float | None = None
    description: str = ""

    @property
    def elapsed(self) -> float:
        return period_start_elapsed(self.period) + period_length(self.period) - self.clock


def period_length(period: int) -> int:
    return PERIOD_SEC if period <= REGULATION_PERIODS else OT_SEC


def period_start_elapsed(period: int) -> int:
    if period <= REGULATION_PERIODS:
        return (period - 1) * PERIOD_SEC
    return REGULATION_PERIODS * PERIOD_SEC + (period - REGULATION_PERIODS - 1) * OT_SEC


def elapsed_to_clock(elapsed: float) -> tuple[int, float]:
    """경기 경과 시간(초) → (피리어드, 남은 시간)."""
    period = 1
    while elapsed >= period_start_elapsed(period) + period_length(period):
        period += 1
    return period, period_start_elapsed(period) + period_length(period) - elapsed


def period_label(period: int) -> str:
    return f"Q{period}" if period <= REGULATION_PERIODS else f"OT{period - REGULATION_PERIODS}"


def fmt_clock(sec: float) -> str:
    sec = max(sec, 0.0)
    if sec < 60:
        return f"{sec:.1f}"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


_ISO_CLOCK = re.compile(r"PT(\d+)M([\d.]+)S")


def parse_iso_clock(s: str) -> float:
    m = _ISO_CLOCK.match(s or "")
    return int(m.group(1)) * 60 + float(m.group(2)) if m else 0.0


def playoff_title(game_id: str) -> str:
    """'0042500405' → '2026 NBA 파이널 5차전' 같은 제목."""
    season = 2000 + int(game_id[3:5]) + 1
    if game_id.startswith("004"):
        rnd, game = int(game_id[7]), int(game_id[9])
        names = {1: "플레이오프 1라운드", 2: "컨퍼런스 준결승", 3: "컨퍼런스 결승", 4: "NBA 파이널"}
        return f"{season} {names.get(rnd, '플레이오프')} {game}차전"
    if game_id.startswith("002"):
        return f"{season - 1}-{str(season)[2:]} 정규시즌"
    return f"경기 {game_id}"
