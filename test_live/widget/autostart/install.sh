#!/bin/zsh
# Mac 로그인 시 test_live 서버와 미니 위젯(감시 프로그램)이 자동으로 켜지게 등록한다.
#   등록:  zsh widget/autostart/install.sh
#   해제:  zsh widget/autostart/uninstall.sh
# macOS 는 ~/Library/LaunchAgents 에 있는 설정 파일만 로그인 때 읽으므로, 거기에 아래 두 파일만 만든다.
#   com.knicks.testlive.server.plist   서버 (꺼지면 30초 뒤 다시 켬)
#   com.knicks.testlive.widget.plist   위젯 (메뉴에서 '위젯 종료' 를 누르면 다음 로그인까지 꺼진 채로)
set -e
LIVE="$(cd "$(dirname "$0")/../.." && pwd)"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
WPY="$LIVE/.venv-widget/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$LIVE/logs/widget"

if [[ ! -x "$WPY" ]]; then
  echo "위젯용 파이썬 환경 만드는 중…"
  "$PY" -m venv "$LIVE/.venv-widget"
  "$WPY" -m pip install -q pyobjc-core pyobjc-framework-Cocoa
fi

# 손으로 켜 둔 서버가 8766 을 쓰고 있으면 자동 실행 서버가 켜지지 못한다
if lsof -nP -iTCP:8766 -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "gui/$UID/com.knicks.testlive.server" >/dev/null 2>&1; then
  echo "⚠️  8766 포트를 이미 다른 서버가 쓰고 있습니다. 터미널에서 켠 app.py 를 Ctrl+C 로 끈 뒤 다시 실행해 주세요."
  lsof -nP -iTCP:8766 -sTCP:LISTEN
  exit 1
fi

write_plist() {  # 이름, KeepAlive 내용, 프로그램 인자들...
  local name=$1 keep=$2; shift 2
  local args=""
  for a in "$@"; do args+="    <string>$a</string>\n"; done
  cat > "$AGENTS/$name.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$name</string>
  <key>ProgramArguments</key>
  <array>
$(printf "$args")
  </array>
  <key>WorkingDirectory</key><string>$LIVE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>$keep
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string><key>LANG</key><string>ko_KR.UTF-8</string></dict>
  <key>StandardOutPath</key><string>$LIVE/logs/$name.log</string>
  <key>StandardErrorPath</key><string>$LIVE/logs/$name.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$UID/$name" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$AGENTS/$name.plist"
  echo "등록: $AGENTS/$name.plist"
}

write_plist com.knicks.testlive.server "<true/>" "$PY" "$LIVE/app.py" --no-open
write_plist com.knicks.testlive.widget "<dict><key>SuccessfulExit</key><false/></dict>" "$WPY" "$LIVE/widget/widget.py"
echo "완료. 지금 바로 켜졌고, 다음 로그인부터 자동으로 켜집니다."
echo "기록: $LIVE/logs/com.knicks.testlive.*.log, $LIVE/logs/widget/widget.log"
