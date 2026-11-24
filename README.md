# Knicks Dashboard

뉴욕 닉스 경기를 한 페이지에서 따라가는 개인 대시보드.
경기 전 프리뷰부터 라이브 추적, 경기 후 분석과 리플레이까지 한 화면 흐름으로 이어진다.

## 화면 구성

**[지금 경기]**
- 팁오프 1시간 전까지: 오늘의 브리핑(부상자·예상 선발·뉴스), 승부 예측, 지난 맞대결 비교, 일정·체력 지표(휴식일·백투백·이동 거리)
- 그 뒤: 라이브 추적 — 점수 흐름, 플레이, 선수 기록, 실책 타임라인
- 경기가 끝나고 15분이 지나면 다음 경기 프리뷰로 자동 전환

**[지난 경기] [전전 경기]**
- 리플레이 (1x / 5x / 10x / 30x, 일시정지) — 라이브와 같은 엔진·같은 화면
- 경기 후 분석: Four Factors, 결정적 런, 클러치, 실책 분석(실책 기반 득점·몰림 구간·선수별)

**리그 데스크**
- 전체 순위, 파워랭킹, 오늘 볼만한 경기, 기록 임박, 신인왕 레이스, 라이벌 감시

## 데이터와 안정성

- 데이터: NBA 공식(cdn.nba.com, stats.nba.com via `nba_api`)과 ESPN 공개 API
- **대체 경로**: cdn.nba.com 이 막히면 ESPN → stats.nba.com 순서로 자동 전환, 다시 열리면 자동 복귀 (`espn_source.py`)
- **요청 관리**: 모든 외부 요청이 `nba_http.py` 한 곳을 지나며 사이트별 분당 요청 수를 제한하고, 끝난 경기 데이터는 영구 캐시
- **실패 처리**: 요청이 실패하면 마지막으로 성공한 데이터를 보여 주고 백그라운드에서 재시도 (`resilient.py`)
- **출처 기반**: 선발 라인업 등은 추측하지 않고 공식·기사 출처가 있는 것만 출처와 함께 표시 (`lineups.py`)

## 실행

```bash
pip install -r requirements.txt
python3 test_live/app.py            # http://127.0.0.1:8766
```

옵션: `--port 8766`, `--team NYK`, `--no-open`(브라우저 자동으로 열지 않음).
같은 와이파이의 휴대폰에서도 접속할 수 있다.

## 폴더 구조

```
test_live/
  app.py          서버 · 경기 자동 선택 · 화면 데이터 조립
  tracker.py      라이브/리플레이 공통 엔진
  nba_http.py     외부 요청 관리 (요청 수 제한 · 영구 캐시)
  resilient.py    실패 처리 · 재시도
  espn_source.py  ESPN 대체 경로
  briefing.py lineups.py desk.py league.py rivals.py
  postgame.py turnovers.py fatigue.py     기능별 모듈
  web/index.html  화면
  widget/         macOS 미니 위젯 (선택)
  replay_data/    리플레이 데모용 경기 기록
```

## AI 에이전트와 함께 개발하기

이 프로젝트는 Claude Code로 기능을 추가하며 만든다.
에이전트가 지켜야 할 규칙(쓰기 범위, 외부 요청 경로, 확인 절차 등)은 [`CLAUDE.md`](CLAUDE.md)에 명문화해 두었다.
