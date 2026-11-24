"""경기 중에만 화면에 떠 있는 미니 위젯 (macOS).

지금 돌아가는 test_live 서버(app.py)의 /api/games, /api/view 를 읽어 작은 창에 보여 준다.
  점수 · 쿼터와 남은 시간 · 승리 확률 막대 · 최근 플레이 1줄 · 새 알림은 1줄로 잠깐 강조

- 팁오프 10분 전에 나타나고, 경기가 끝나면 최종 점수를 5분 보여 준 뒤 닫힌다. 경기 없는 날은 뜨지 않는다.
- 항상 다른 앱 위에, 모든 데스크톱(Space)과 전체화면 앱 위에도 보인다.
- 드래그로 이동, 오른쪽 아래 ◢ 로 크기 조절, ◐ 로 투명도, × 로 숨기기. 위치·크기·투명도는 widget/prefs.json 에 저장.
- 창을 클릭(드래그 없이)하면 전체 대시보드를 연다. 경기 중에는 메뉴 막대에 🏀 메뉴가 생긴다 (다시 보이기·투명도·종료).
- 서버가 응답하지 않으면 '연결 끊김' 을 표시하고 마지막 값을 흐리게 둔다.

실행:  .venv-widget/bin/python widget/widget.py
시험:  --channel last        지난 경기 리플레이를 따라간다 (재생 버튼을 누르면 뜨고, 끝나면 최종 점수 뒤 닫힘)
       --final-hold 초       최종 점수를 보여 줄 시간 (기본 300)
       WIDGET_NOW=2026-10-20T22:50:00Z   가짜 현재 시각 (그 시각부터 실제 속도로 흐른다)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DIR = os.path.join(ROOT, "logs", "widget")
PREFS = os.path.join(HERE, "prefs.json")

LEAD = timedelta(minutes=10)        # 팁오프 몇 분 전에 나타날지
FINAL_HOLD = 300                    # 최종 점수를 보여 줄 초
ALERT_SEC = 8                       # 새 알림 강조 시간
SIZE = (320, 132)                   # 기본 크기
MIN_SIZE, MAX_SIZE = (260, 116), (640, 264)
ALPHAS = (1.0, 0.85, 0.7, 0.55, 0.4)

TEAM_HEX = {"NYK": "#F58426", "ATL": "#E03A3E", "BKN": "#C9CDD3", "BOS": "#2BC47A", "CHA": "#00A3AD", "CHI": "#E2163D",
            "CLE": "#C9466F", "DAL": "#2E86DE", "DEN": "#4F7FD8", "DET": "#E0263F", "GSW": "#3D6CC8", "HOU": "#E2163D",
            "IND": "#3F6FBF", "LAC": "#E0263F", "LAL": "#8D5CC9", "MEM": "#7D97CC", "MIA": "#E0243F", "MIL": "#2E8B4A",
            "MIN": "#5B8DEF", "NOP": "#D32F45", "OKC": "#2A9DE0", "ORL": "#2B8FD6", "PHI": "#3E8EDE", "PHX": "#7B5CC9",
            "POR": "#E03A3E", "SAC": "#8E5CC9", "SAS": "#C4CED4", "TOR": "#E2163D", "UTA": "#9466DB", "WAS": "#E31837"}
KST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- 시각 (시험용 가짜 시각 지원)

_FAKE = os.environ.get("WIDGET_NOW")
_FAKE_BASE = (datetime.fromisoformat(_FAKE.replace("Z", "+00:00")), time.monotonic()) if _FAKE else None


def now_utc() -> datetime:
    if _FAKE_BASE:
        return _FAKE_BASE[0] + timedelta(seconds=time.monotonic() - _FAKE_BASE[1])
    return datetime.now(timezone.utc)


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def log(msg: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(LOG_DIR, "widget.log"), "a") as f:
        f.write(line + "\n")


def load_prefs(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prefs(path: str, prefs: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(prefs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 언제 띄울지 (화면과 무관한 순수 로직)

class Watcher:
    """서버에서 받은 경기 정보로 '지금 위젯을 띄울지, 어떤 단계인지' 를 정한다.

    단계: pregame(팁오프 10분 전~) → live → final(최종 점수, hold 초) → 닫힘.
    한 번 다 보여 준 경기는 done 에 넣어 다시 띄우지 않는다 (위젯을 다시 켜도 마찬가지).
    한 번도 진행 중인 모습을 못 본 채 이미 끝난 경기(예: 경기 뒤 Mac 로그인)는 띄우지 않는다.
    """

    def __init__(self, channel: str = "now", lead: timedelta = LEAD, hold: float = FINAL_HOLD, done=()):
        self.channel, self.lead, self.hold = channel, lead, hold
        self.done: list[str] = list(done)
        self.seen_active: set[str] = set()
        self.final_at: dict[str, datetime] = {}
        self.session: str | None = None      # 지금 보여 주는 경기(리플레이는 재생 한 번) 키
        self.phase: str | None = None
        self.last_game: dict | None = None   # 마지막으로 받은 경기 정보 (서버가 꺼졌을 때 판단용)
        self.last_view: dict | None = None

    def wants_view(self, game: dict | None, now: datetime) -> bool:
        """가벼운 /api/games 만으로 충분한지, 무거운 /api/view 도 받아야 하는지."""
        if self.session or self.channel != "now":
            return True
        return bool(game) and now >= parse_utc(game["tipoff_utc"]) - self.lead - timedelta(minutes=1)

    def _hide(self, why: str) -> dict:
        return {"show": False, "why": why}

    def _show(self, phase: str, offline: bool = False, **kw) -> dict:
        return {"show": True, "phase": phase, "key": self.session, "offline": offline,
                "game": self.last_game, "view": self.last_view, **kw}

    def step(self, now: datetime, game: dict | None, view: dict | None, ok: bool) -> dict:
        # 1) 최종 점수를 보여 주는 중이면 hold 를 다 채울 때까지 (서버가 꺼져도) 그대로 둔다
        if self.session in self.final_at:
            left = self.hold - (now - self.final_at[self.session]).total_seconds()
            if left > 0:
                return self._show("final", offline=not ok, final_left=left)
            self.done = (self.done + [self.session])[-50:]
            del self.final_at[self.session]
            self.session = None
            return self._hide("최종 점수 표시 끝")
        # 2) 서버 응답 없음: 보여 주던 경기는 '연결 끊김' 으로 유지, 아니면 마지막 일정으로 판단
        if not ok:
            if self.session:
                return self._show(self.phase, offline=True)
            g = self.last_game
            if self.channel == "now" and g and g["game_id"] not in self.done:
                tip = parse_utc(g["tipoff_utc"])
                if tip - self.lead <= now < tip + timedelta(hours=4):
                    self.session, self.phase = g["game_id"], "pregame"
                    self.seen_active.add(self.session)
                    return self._show("pregame", offline=True)
            return self._hide("서버 응답 없음")
        # 서버는 경기가 끝나면 '지금 경기' 를 곧바로 다음 경기로 바꾼다. 보던 경기가 사라지면
        # 마지막으로 받은 점수를 최종 점수로 hold 초 보여 준다
        if (self.channel == "now" and self.session and self.phase == "live"
                and (not game or game["game_id"] != self.session)):
            self.final_at[self.session] = now
            return self._show("final", final_left=self.hold)
        self.last_game = game
        if view is not None:
            self.last_view = view
        if not game:
            self.session = None
            return self._hide("경기 없음")
        st = ((view or {}).get("state") or {}).get("status")
        if self.channel == "now":
            key = game["game_id"]
            final = st == "final" or game.get("status") == "종료"
            tip = parse_utc(game["tipoff_utc"])
            active = not final and (game.get("status") == "LIVE" or st == "live" or now >= tip - self.lead)
        else:   # 리플레이: 재생 버튼을 누른 뒤부터. 다시 재생하면 새 기록 폴더가 생기므로 새 세션
            if not view or view.get("kind") != "replay":
                self.session = None
                return self._hide("리플레이 재생 전")
            key = f"{game['game_id']}|{(view.get('meta') or {}).get('log_dir')}"
            final, active = st == "final", st == "live"
        if key in self.done:
            self.session = None
            return self._hide("이미 보여 준 경기")
        if final:
            if key in self.seen_active:
                self.session = key
                self.final_at[key] = now
                return self._show("final", final_left=self.hold)
            self.done = (self.done + [key])[-50:]
            self.session = None
            return self._hide("진행 중인 모습을 못 본 채 끝난 경기")
        if active:
            self.session = key
            self.seen_active.add(key)
            self.phase = "live" if st == "live" else "pregame"
            return self._show(self.phase)
        self.session = None
        return self._hide("팁오프 10분 전이 아님" if self.channel == "now" else "리플레이 준비 중")


# ---------------------------------------------------------------- 표시할 글자 만들기 (순수 함수)

def qlabel(period: int, clock: str) -> str:
    q = f"{period}Q" if period <= 4 else "OT" if period == 5 else f"{period - 4}OT"
    return f"{q} {clock}".strip()


def fmt_clock(sec: float) -> str:
    return f"{sec:.1f}" if sec < 60 else f"{int(sec) // 60}:{int(sec) % 60:02d}"


def live_clock(s: dict, view: dict) -> tuple[str, float | None]:
    """대시보드와 같은 시계 자동 진행: 서버의 clock_run 이 running 이면 다음 데이터가 올 때까지 1초씩 줄인다.
    (멈춤·쿼터 사이·하프타임·0초에서는 진행하지 않음, 중계 딜레이만큼 늦춘 화면 기준) → (시계 글자, 마지막 데이터 뒤 초)"""
    r = s.get("clock_run")
    if not r:
        return s.get("clock", ""), None
    meta = view.get("meta") or {}
    try:   # 서버 시각 기준 지금 = 받은 순간의 server_utc + 그 뒤 흐른 시간
        srv_now = parse_utc(view["server_utc"]).timestamp() + (time.time() - view.get("_got_at", time.time()))
        age = max(0.0, srv_now - float(meta.get("delay_sec") or 0) - parse_utc(r["at"]).timestamp())
    except (KeyError, ValueError, TypeError):
        return s.get("clock", ""), None
    if not r.get("running") or s.get("status") != "live" or s.get("break"):
        return s.get("clock", ""), age
    ran = int(min(age, r["hold_sec"]) * r["rate"])
    return (fmt_clock(max(0.0, r["sec"] - ran)) if ran > 0 else s.get("clock", "")), age


def age_ko(sec: float) -> str:
    return f"{int(sec)}초 전" if sec < 60 else f"{int(sec // 60)}분 전"


def hex_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def compose(d: dict, now: datetime, focus: str = "NYK") -> dict:
    """Watcher 결과 → 화면에 쓸 글자와 숫자."""
    g, view = d.get("game") or {}, d.get("view") or {}
    s = view.get("state") or {}
    home, away = s.get("home") or g.get("home", ""), s.get("away") or g.get("away", "")
    if not home and g:
        home, away = (focus, g["opp"]) if g.get("home") else (g["opp"], focus)
    score = s.get("score") or {}
    out = {"home": home, "away": away, "hs": "", "as": "", "center": "", "top": "", "line": "", "p_home": None,
           "p_label": "", "dim": d.get("offline", False)}
    kind = (g.get("type") or "")
    phase = d["phase"]
    if phase == "pregame" or s.get("status") in ("preview", "ready", None):
        tip = parse_utc(g["tipoff_utc"]) if g.get("tipoff_utc") else None
        mins = int((tip - now).total_seconds() // 60) if tip else None
        out["center"] = "곧 시작" if mins is None or mins <= 0 else f"팁오프 {mins + 1}분 전"
        out["top"] = " · ".join(x for x in (kind, tip.astimezone(KST).strftime("%H:%M 시작") if tip else "") if x)
        out["line"] = g.get("arena", "")
        more = (view.get("prediction_more") or {})
        for src, name in (((more.get("odds") or {}), "배당"), ((more.get("espn") or {}), "ESPN"),
                          ((view.get("prediction") or {}), "넷레이팅")):
            if src.get("home_win") is not None:
                out["p_home"], out["p_label"] = src["home_win"], f"경기 전 예측({name})"
                break
    else:
        out["hs"], out["as"] = str(score.get(home, 0)), str(score.get(away, 0))
        if s.get("status") == "final" or phase == "final":
            out["center"] = "경기 종료"
            left = d.get("final_left") or 0
            out["top"] = f"최종 점수 · {max(1, round(left / 60))}분 뒤 닫힘" if left >= 60 else f"최종 점수 · {int(left)}초 뒤 닫힘"
        else:
            if s.get("break"):
                out["center"] = "하프타임" if s["break"] == "하프타임" else f"{qlabel(s.get('period', 1), '')} 종료"
            clock, age = live_clock(s, view)
            if not s.get("break"):
                out["center"] = qlabel(s.get("period", 1), clock)
            mode = (view.get("meta") or {}).get("mode")
            out["top"] = " · ".join(x for x in (kind, "리플레이" if mode == "replay" else "LIVE",
                                                age_ko(age) + " 업데이트" if age is not None else "") if x)
        wp = s.get("win_prob") or {}
        if wp.get("current") is not None:
            out["p_home"], out["p_label"] = wp["current"], "승리 확률"
        plays = s.get("plays") or []
        if s.get("status") == "final" or phase == "final":
            out["line"] = "클릭하면 전체 대시보드"
        elif plays:
            p = plays[0]
            lab = p.get("label", "")
            if lab.startswith("Q") and " " in lab:
                q, c = lab[1:].split(" ", 1)
                lab = qlabel(int(q), c) if q.isdigit() else lab
            out["line"] = " ".join(x for x in (lab, p.get("team", ""), "·", p.get("desc", "")) if x)
    if d.get("offline"):
        out["top"] = "연결 끊김 · 서버 응답 없음"
    if out["p_home"] is not None:
        who = focus if focus in (home, away) else home
        p = out["p_home"] if who == home else 1 - out["p_home"]
        out["p_text"] = f"{out['p_label']} {who} {round(p * 100)}%"
    else:
        out["p_text"] = ""
    return out


# ---------------------------------------------------------------- 서버에서 받기

def fetch(url: str, timeout: float = 4) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-store"}), timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- 화면 (AppKit)

def run_ui(args) -> None:
    import objc
    from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered, NSBezierPath,
                        NSColor, NSEvent, NSFont, NSImage, NSMenu, NSMenuItem, NSPanel, NSScreen, NSStatusBar,
                        NSTextField, NSView, NSVisualEffectView, NSWorkspace, NSAppearance)
    from Foundation import NSURL, NSObject, NSMakeRect, NSMakeSize, NSPointInRect
    from PyObjCTools import AppHelper

    BORDERLESS, RESIZABLE, NONACTIVATING = 0, 1 << 3, 1 << 7
    STATUS_LEVEL = 25    # NSStatusWindowLevel: 일반 창·전체화면 앱보다 위
    ALL_SPACES, STATIONARY, IGNORES_CYCLE, FULLSCREEN_AUX = 1 << 0, 1 << 4, 1 << 6, 1 << 8
    DRAGGED, MOUSE_UP = 1 << 6, 1 << 2     # NSEventMaskLeftMouseDragged / LeftMouseUp
    TRUNC = 4                               # NSLineBreakByTruncatingTail

    prefs_path = args.prefs
    prefs = load_prefs(prefs_path)
    watcher = Watcher(args.channel, timedelta(seconds=args.lead), args.final_hold, prefs.get("done", []))
    base = args.server.rstrip("/")

    def rgb(h, a=1.0):
        r, g, b = hex_rgb(h)
        return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)

    def make_label(align=0):
        t = NSTextField.labelWithString_("")
        t.setAlignment_(align)
        t.setLineBreakMode_(TRUNC)
        t.setTextColor_(NSColor.whiteColor())
        t.cell().setTruncatesLastVisibleLine_(True)
        return t

    def rounded_mask(r):
        def draw(rect):
            NSColor.blackColor().set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, r, r).fill()
            return True
        img = NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(2 * r + 1, 2 * r + 1), False, draw)
        img.setCapInsets_((r, r, r, r))
        img.setResizingMode_(1)   # NSImageResizingModeStretch
        return img

    class Bar(NSView):
        """승리 확률 막대: 왼쪽 원정, 오른쪽 홈. 사이에 2px 틈."""

        def initWithFrame_(self, frame):
            self = objc.super(Bar, self).initWithFrame_(frame)
            self.p, self.left, self.right = None, "#8FA3BF", "#8FA3BF"
            return self

        def drawRect_(self, rect):
            b = self.bounds()
            w, h = b.size.width, b.size.height
            r = h / 2
            if self.p is None:
                NSColor.colorWithWhite_alpha_(1, 0.12).set()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, r, r).fill()
                return
            split = max(0.0, min(1.0, 1 - self.p)) * w
            if split > 1:
                rgb(self.left).set()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(0, 0, max(split - 1, h), h), r, r).fill()
            if w - split > 1:
                rgb(self.right).set()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(min(split + 1, w - h), 0, max(w - split - 1, h), h), r, r).fill()

    class Root(NSView):
        """창 전체. 클릭은 모두 여기서 받는다 (글자 칸이 마우스를 가로채지 않게)."""

        def hitTest_(self, p):
            return self if NSPointInRect(self.convertPoint_fromView_(p, self.superview()), self.bounds()) else None

        def acceptsFirstMouse_(self, ev):
            return True

        def isFlipped(self):
            return True   # 위에서부터 배치

        def mouseDown_(self, ev):
            self.owner.mouse_down(self, ev)

        def setFrameSize_(self, size):
            objc.super(Root, self).setFrameSize_(size)
            if getattr(self, "owner", None):
                self.owner.relayout()

    class Menu(NSObject):
        def toggleShow_(self, sender):
            ui.user_toggle()

        def openDash_(self, sender):
            ui.open_dashboard()

        def setAlpha_(self, sender):
            ui.set_alpha(sender.representedObject())

        def resetPos_(self, sender):
            ui.reset_frame()

        def quit_(self, sender):
            log("메뉴에서 위젯 종료")
            os._exit(0)

    class UI:
        def __init__(self):
            self.panel = None
            self.status_item = None
            self.menu_target = Menu.alloc().init()
            self.user_hidden_for: str | None = None   # × 로 숨긴 경기
            self.shown_key: str | None = None
            self.alert_seen: set[str] = set()
            self.alert_text, self.alert_until = "", 0.0
            self.last = None
            self.last_ok_at: str | None = None
            self.alpha = float(prefs.get("alpha", 1.0))
            self.build()

        # ---- 창 만들기
        def default_frame(self):
            vis = NSScreen.mainScreen().visibleFrame()
            w, h = SIZE
            return (vis.origin.x + vis.size.width - w - 20, vis.origin.y + vis.size.height - h - 20, w, h)

        def saved_frame(self):
            f = prefs.get("frame")
            if not f:
                return self.default_frame()
            x, y, w, h = f
            w, h = max(MIN_SIZE[0], min(w, MAX_SIZE[0])), max(MIN_SIZE[1], min(h, MAX_SIZE[1]))
            # 모니터를 뺐을 때처럼 저장된 위치가 화면 밖이면 기본 위치로
            for sc in NSScreen.screens():
                v = sc.visibleFrame()
                if v.origin.x <= x + w / 2 <= v.origin.x + v.size.width and v.origin.y <= y + h / 2 <= v.origin.y + v.size.height:
                    return (x, y, w, h)
            return self.default_frame()

        def build(self):
            x, y, w, h = self.saved_frame()
            p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(x, y, w, h), BORDERLESS | RESIZABLE | NONACTIVATING, NSBackingStoreBuffered, False)
            p.setFloatingPanel_(True)          # (이게 창 높이를 '떠 있는 창' 으로 되돌리므로 높이보다 먼저)
            p.setLevel_(STATUS_LEVEL)
            p.setCollectionBehavior_(ALL_SPACES | STATIONARY | IGNORES_CYCLE | FULLSCREEN_AUX)
            p.setHidesOnDeactivate_(False)
            p.setCanHide_(False)
            p.setOpaque_(False)
            p.setBackgroundColor_(NSColor.clearColor())
            p.setHasShadow_(True)
            p.setMinSize_(MIN_SIZE)
            p.setMaxSize_(MAX_SIZE)
            p.setAlphaValue_(self.alpha)
            p.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
            root = Root.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
            root.owner = self
            root.setToolTip_("드래그: 이동 · 클릭: 대시보드 열기 · ◢: 크기 조절 · ◐: 투명도 · ×: 숨기기")
            bg = NSVisualEffectView.alloc().initWithFrame_(root.bounds())
            bg.setMaterial_(13)        # HUD 창
            bg.setBlendingMode_(0)     # 뒤 창과 섞임
            bg.setState_(1)            # 항상 활성
            bg.setMaskImage_(rounded_mask(12))
            bg.setAutoresizingMask_(2 | 16)
            root.addSubview_(bg)
            self.bg = bg
            L = self.lab = {}
            for k, align in (("top", 0), ("alpha", 1), ("hide", 1), ("away", 0), ("as", 0), ("center", 1), ("hs", 2),
                             ("home", 2), ("p_text", 1), ("line", 0), ("alert", 0), ("grip", 2)):
                L[k] = make_label(align)
                root.addSubview_(L[k])
            L["alpha"].setStringValue_("◐")
            L["hide"].setStringValue_("×")
            L["grip"].setStringValue_("◢")
            L["grip"].setTextColor_(NSColor.colorWithWhite_alpha_(1, 0.35))
            L["alert"].setDrawsBackground_(True)
            L["alert"].setBackgroundColor_(rgb("#F58426", 0.92))
            L["alert"].setTextColor_(NSColor.blackColor())
            L["alert"].setHidden_(True)
            self.bar = Bar.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 8))
            root.addSubview_(self.bar)
            p.setContentView_(root)
            self.panel, self.root = p, root
            self.relayout()

        def relayout(self):
            if not getattr(self, "root", None):
                return
            b = self.root.bounds()
            W, H = b.size.width, b.size.height
            s = max(0.8, min(W / SIZE[0], H / SIZE[1], 2.0))
            pad = 12 * s
            fnt = lambda size, weight=0.0: NSFont.monospacedDigitSystemFontOfSize_weight_(size * s, weight)
            L = self.lab
            y = 7 * s
            L["top"].setFont_(fnt(10.5))
            L["top"].setFrame_(NSMakeRect(pad, y, W - pad * 2 - 44 * s, 15 * s))
            for k, x in (("alpha", W - pad - 40 * s), ("hide", W - pad - 18 * s)):
                L[k].setFont_(fnt(13, 0.3))
                L[k].setFrame_(NSMakeRect(x, y - 2 * s, 20 * s, 18 * s))
            self.alpha_rect = NSMakeRect(W - pad - 44 * s, 0, 24 * s, 26 * s)
            self.hide_rect = NSMakeRect(W - pad - 20 * s, 0, 20 * s + pad, 26 * s)
            y += 18 * s
            rowh = 32 * s
            third = (W - pad * 2) / 3
            L["away"].setFont_(fnt(13, 0.5))
            L["away"].setFrame_(NSMakeRect(pad, y + 8 * s, 40 * s, 18 * s))
            L["as"].setFont_(fnt(26, 0.6))
            L["as"].setFrame_(NSMakeRect(pad + 38 * s, y - 2 * s, third, rowh))
            L["center"].setFont_(fnt(13, 0.4))
            L["center"].setFrame_(NSMakeRect(pad + third * 0.55, y + 8 * s, third * 1.9, 18 * s))
            L["hs"].setFont_(fnt(26, 0.6))
            L["hs"].setFrame_(NSMakeRect(W - pad - 38 * s - third, y - 2 * s, third, rowh))
            L["home"].setFont_(fnt(13, 0.5))
            L["home"].setFrame_(NSMakeRect(W - pad - 40 * s, y + 8 * s, 40 * s, 18 * s))
            L["center"].removeFromSuperview()
            self.root.addSubview_(L["center"])   # 가운데 글자가 점수보다 위에 오게
            y += rowh + 4 * s
            L["p_text"].setFont_(fnt(10.5))
            L["p_text"].setFrame_(NSMakeRect(pad, y, W - pad * 2, 14 * s))
            y += 16 * s
            self.bar.setFrame_(NSMakeRect(pad, y, W - pad * 2, 7 * s))
            self.bar.setNeedsDisplay_(True)
            y += 7 * s + 8 * s
            for k in ("line", "alert"):
                L[k].setFont_(fnt(11.5, 0.2 if k == "alert" else 0.0))
                L[k].setFrame_(NSMakeRect(pad - (4 * s if k == "alert" else 0), y, W - pad * 2 + (8 * s if k == "alert" else 0) - 14 * s, 17 * s))
            L["grip"].setFont_(fnt(10))
            L["grip"].setFrame_(NSMakeRect(W - 18 * s, H - 16 * s, 14 * s, 14 * s))
            self.grip_rect = NSMakeRect(W - 22 * s, H - 20 * s, 22 * s, 20 * s)

        # ---- 마우스: 이동·크기 조절·클릭 (직접 추적해서 드래그와 클릭을 구분)
        def mouse_down(self, view, ev):
            p = view.convertPoint_fromView_(ev.locationInWindow(), None)
            if NSPointInRect(p, self.hide_rect):
                return self.user_hide()
            if NSPointInRect(p, self.alpha_rect):
                return self.cycle_alpha()
            mode = "resize" if NSPointInRect(p, self.grip_rect) else "move"
            win = self.panel
            start, f0 = NSEvent.mouseLocation(), win.frame()
            moved = False
            while True:
                e = win.nextEventMatchingMask_(DRAGGED | MOUSE_UP)
                cur = NSEvent.mouseLocation()
                dx, dy = cur.x - start.x, cur.y - start.y
                moved = moved or abs(dx) + abs(dy) > 3
                if e.type() == 2:    # NSEventTypeLeftMouseUp
                    break
                if not moved:
                    continue
                if mode == "move":
                    win.setFrameOrigin_((f0.origin.x + dx, f0.origin.y + dy))
                else:   # 왼쪽 위는 그대로, 오른쪽 아래를 끈다
                    nw = max(MIN_SIZE[0], min(f0.size.width + dx, MAX_SIZE[0]))
                    nh = max(MIN_SIZE[1], min(f0.size.height - dy, MAX_SIZE[1]))
                    win.setFrame_display_(NSMakeRect(f0.origin.x, f0.origin.y + f0.size.height - nh, nw, nh), True)
            if moved:
                self.save_frame()
            elif mode == "move":
                self.open_dashboard()

        def save_frame(self):
            f = self.panel.frame()
            prefs["frame"] = [round(f.origin.x), round(f.origin.y), round(f.size.width), round(f.size.height)]
            save_prefs(prefs_path, prefs)
            log(f"위치 저장 {prefs['frame']}")

        def reset_frame(self):
            x, y, w, h = self.default_frame()
            self.panel.setFrame_display_(NSMakeRect(x, y, w, h), True)
            self.save_frame()

        def set_alpha(self, a):
            self.alpha = float(a)
            self.panel.setAlphaValue_(self.alpha)
            prefs["alpha"] = self.alpha
            save_prefs(prefs_path, prefs)
            self.rebuild_menu()

        def cycle_alpha(self):
            i = min(range(len(ALPHAS)), key=lambda j: abs(ALPHAS[j] - self.alpha))
            self.set_alpha(ALPHAS[(i + 1) % len(ALPHAS)])

        def open_dashboard(self):
            url = f"{base}/#{args.channel}"
            log(f"대시보드 열기 {url}")
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

        def user_hide(self):
            self.user_hidden_for = self.shown_key
            self.panel.orderOut_(None)
            log("× 로 숨김 (메뉴 막대 🏀 → 위젯 보이기로 다시 표시)")
            self.rebuild_menu()
            self.dump()

        def user_toggle(self):
            if self.panel.isVisible():
                return self.user_hide()
            self.user_hidden_for = None
            if self.last and self.last.get("show"):
                self.panel.orderFrontRegardless()
            self.rebuild_menu()
            self.dump()

        # ---- 메뉴 막대 🏀 (경기 중에만)
        def rebuild_menu(self):
            if not self.status_item:
                return
            m = NSMenu.alloc().init()

            def item(title, sel, rep=None, state=False):
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
                it.setTarget_(self.menu_target)
                if rep is not None:
                    it.setRepresentedObject_(rep)
                it.setState_(1 if state else 0)
                m.addItem_(it)
            item("위젯 숨기기" if self.panel.isVisible() else "위젯 보이기", "toggleShow:")
            item("대시보드 열기", "openDash:")
            m.addItem_(NSMenuItem.separatorItem())
            for a in ALPHAS:
                item(f"투명도 {round((1 - a) * 100)}%", "setAlpha:", a, abs(a - self.alpha) < 0.01)
            m.addItem_(NSMenuItem.separatorItem())
            item("위치·크기 초기화", "resetPos:")
            item("위젯 종료", "quit:")
            self.status_item.setMenu_(m)

        def set_status_item(self, on):
            if on and not self.status_item:
                self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
                self.status_item.button().setTitle_("🏀")
                self.rebuild_menu()
            elif not on and self.status_item:
                NSStatusBar.systemStatusBar().removeStatusItem_(self.status_item)
                self.status_item = None

        # ---- 받은 데이터 반영
        def apply(self, d):
            prev = self.last
            self.last = d
            if not d.get("show"):
                if self.panel.isVisible() or (prev and prev.get("show")):
                    log(f"닫힘: {d['why']}")
                self.panel.orderOut_(None)
                self.shown_key = None
                self.set_status_item(False)
                return self.dump()
            key = d["key"]
            if key != self.shown_key:   # 새 경기: 지금까지 쌓인 알림은 강조하지 않는다
                self.shown_key, self.user_hidden_for = key, None
                self.alert_seen = {a.get("key") for a in self._alerts(d)}
                self.alert_text, self.alert_until = "", 0.0
                log(f"나타남: {key} ({d['phase']})")
            elif prev and prev.get("phase") != d["phase"]:
                log(f"단계 바뀜: {prev.get('phase')} → {d['phase']}")
            if prev and prev.get("show") and bool(prev.get("offline")) != bool(d.get("offline")):
                log("연결 끊김" if d.get("offline") else "다시 연결됨")
            for a in self._alerts(d):
                if a.get("key") in self.alert_seen:
                    continue
                self.alert_seen.add(a.get("key"))
                if not a.get("quiet") and not d.get("offline"):   # 건너뛴 구간 알림은 조용히
                    self.alert_text = "🔔 " + " · ".join(x for x in (a.get("title"), a.get("body")) if x)
                    self.alert_until = time.time() + ALERT_SEC
                    AppHelper.callLater(ALERT_SEC + 0.1, self.refresh_alert)
                    log(f"알림 강조: {self.alert_text}")
            self.paint(d)
            self.set_status_item(True)
            if self.user_hidden_for != key and not self.panel.isVisible():
                self.panel.orderFrontRegardless()
            self.rebuild_menu()
            self.dump()

        def _alerts(self, d):
            return (((d.get("view") or {}).get("state") or {}).get("alerts")) or []

        def refresh_alert(self):
            if self.last and self.last.get("show"):
                self.paint(self.last)
                self.dump()

        def tick_clock(self):
            """1초마다 다시 그린다 — 경기 시계 자동 진행과 '○초 전 업데이트' (서버를 다시 읽지는 않는다)."""
            try:
                if self.last and self.last.get("show") and self.panel.isVisible():
                    self.paint(self.last)
            finally:
                AppHelper.callLater(1.0, self.tick_clock)

        def paint(self, d):
            c = compose(d, now_utc())
            self.composed = c
            L = self.lab
            dim = 0.45 if c["dim"] else 1.0
            L["top"].setStringValue_(c["top"])
            L["top"].setTextColor_(rgb("#FF6B6B") if c["dim"] else NSColor.colorWithWhite_alpha_(1, 0.6))
            for k in ("away", "home"):
                L[k].setStringValue_(c[k])
                L[k].setTextColor_(rgb(TEAM_HEX.get(c[k], "#8FA3BF"), dim))
            for k in ("as", "hs", "center", "p_text", "line"):
                L[k].setStringValue_(c[k])
                L[k].setTextColor_(NSColor.colorWithWhite_alpha_(1, (0.75 if k in ("p_text", "line") else 1.0) * dim))
            self.bar.p = c["p_home"]
            self.bar.left, self.bar.right = TEAM_HEX.get(c["away"], "#8FA3BF"), TEAM_HEX.get(c["home"], "#8FA3BF")
            self.bar.setNeedsDisplay_(True)
            on = time.time() < self.alert_until
            L["alert"].setStringValue_(" " + self.alert_text if on else "")
            L["alert"].setHidden_(not on)
            L["line"].setHidden_(on)

        def dump(self):
            """시험·문제 확인용: 지금 창 상태를 logs/widget/status.json 에 남긴다."""
            p = self.panel
            f = p.frame()
            c = getattr(self, "composed", {}) if self.last and self.last.get("show") else {}
            st = {"at": datetime.now().isoformat(timespec="seconds"), "visible": bool(p.isVisible()),
                  "phase": (self.last or {}).get("phase"), "key": (self.last or {}).get("key"),
                  "why": (self.last or {}).get("why"), "offline": (self.last or {}).get("offline"),
                  "texts": {k: c.get(k) for k in ("top", "away", "as", "center", "hs", "home", "p_text", "line")},
                  "alert": self.lab["alert"].stringValue() if not self.lab["alert"].isHidden() else "",
                  "frame": [round(f.origin.x), round(f.origin.y), round(f.size.width), round(f.size.height)],
                  "alpha": round(p.alphaValue(), 2), "level": int(p.level()), "behavior": int(p.collectionBehavior()),
                  "menu_bar_icon": bool(self.status_item), "user_hidden": self.user_hidden_for is not None}
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "status.json"), "w") as fh:
                json.dump(st, fh, ensure_ascii=False, indent=1)
            if args.snapshot and p.isVisible():   # 시험용: 창 모습을 그림 파일로 (화면 녹화 권한 없이)
                rep = self.root.bitmapImageRepForCachingDisplayInRect_(self.root.bounds())
                self.root.cacheDisplayInRect_toBitmapImageRep_(self.root.bounds(), rep)
                rep.representationUsingType_properties_(4, None).writeToFile_atomically_(args.snapshot, True)

    # ---- 감시 스레드: 서버를 읽고 Watcher 로 판단해 화면 스레드에 넘긴다
    def poll_loop():
        while True:
            now = now_utc()
            game = view = None
            try:
                game = fetch(f"{base}/api/games").get(args.channel)
                if watcher.wants_view(game, now):
                    view = fetch(f"{base}/api/view?ch={args.channel}", timeout=6)
                    view["_got_at"] = time.time()   # 시계 자동 진행: 서버 시각과 맞추는 기준
                ok = True
            except Exception:
                ok = False
            d = watcher.step(now, game, view, ok)
            if prefs.get("done") != watcher.done:
                prefs["done"] = watcher.done
                save_prefs(prefs_path, prefs)
            AppHelper.callAfter(ui.apply, d)
            time.sleep(args.poll if d["show"] or args.channel != "now" else args.idle_poll)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)   # Dock 아이콘 없음
    ui = UI()
    AppHelper.callLater(1.0, ui.tick_clock)
    log(f"위젯 감시 시작 (서버 {base}, 채널 {args.channel}, 최종 점수 {args.final_hold:g}초"
        + (f", 가짜 시각 {_FAKE}" if _FAKE else "") + ")")
    threading.Thread(target=poll_loop, daemon=True).start()
    AppHelper.runEventLoop(installInterrupt=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://127.0.0.1:8766")
    ap.add_argument("--channel", default="now", choices=("now", "last", "prev2"))
    ap.add_argument("--final-hold", type=float, default=FINAL_HOLD)
    ap.add_argument("--lead", type=float, default=LEAD.total_seconds(), help="팁오프 몇 초 전에 나타날지")
    ap.add_argument("--poll", type=float, default=3, help="창이 떠 있을 때 서버를 읽는 간격(초)")
    ap.add_argument("--idle-poll", type=float, default=30, help="창이 없을 때 일정을 확인하는 간격(초)")
    ap.add_argument("--prefs", default=PREFS)
    ap.add_argument("--snapshot", help="시험용: 창을 그릴 때마다 이 경로에 PNG 로 저장")
    args = ap.parse_args()
    run_ui(args)


if __name__ == "__main__":
    main()
