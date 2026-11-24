#!/bin/zsh
# 로그인 자동 실행을 끈다: 서버·위젯을 멈추고 ~/Library/LaunchAgents 의 설정 파일 두 개를 지운다.
#   zsh widget/autostart/uninstall.sh            둘 다 끄기
#   zsh widget/autostart/uninstall.sh widget     위젯만 끄기 (서버는 계속 자동 실행)
AGENTS="$HOME/Library/LaunchAgents"
names=(com.knicks.testlive.server com.knicks.testlive.widget)
[[ "$1" == "widget" ]] && names=(com.knicks.testlive.widget)
[[ "$1" == "server" ]] && names=(com.knicks.testlive.server)
for name in $names; do
  launchctl bootout "gui/$UID/$name" 2>/dev/null && echo "멈춤: $name" || echo "이미 꺼져 있음: $name"
  rm -f "$AGENTS/$name.plist" && echo "삭제: $AGENTS/$name.plist"
done
echo "완료. 다음 로그인부터 자동으로 켜지지 않습니다. (서버를 손으로 켜려면: cd test_live && python3 app.py)"
