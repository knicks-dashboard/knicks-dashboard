"""대시보드 서버.

리플레이: python3 -m gamecast.server --replay 0042500405 --speed 10
라이브:   python3 -m gamecast.server --live <GAME_ID>
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .sources import ReplaySource, StatsLiveSource
from .state import build_snapshot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
BASELINE_PATH = os.path.join(ROOT, "baseline", "baseline.json")


def load_baseline(focus: str) -> dict | None:
    if not os.path.exists(BASELINE_PATH):
        return None
    with open(BASELINE_PATH) as f:
        data = json.load(f)
    # 기준값 파일은 닉스 기준. 다른 팀에 초점을 맞추면 비교하지 않는다.
    return data if focus == "NYK" else None


def make_handler(source, focus: str, baseline: dict | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/snapshot":
                events, starters, now = source.current()
                snap = build_snapshot(source.meta, events, starters, now, focus, baseline)
                snap["status"] = source.status()
                return self._json(snap)
            name = "index.html" if path == "/" else path.lstrip("/")
            file = os.path.normpath(os.path.join(WEB_DIR, name))
            if not file.startswith(WEB_DIR) or not os.path.isfile(file):
                return self._json({"error": "not found"}, 404)
            with open(file, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/api/control":
                return self._json({"error": "not found"}, 404)
            length = int(self.headers.get("Content-Length", 0))
            cmd = json.loads(self.rfile.read(length) or b"{}")
            return self._json(source.control(cmd))

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--replay", metavar="GAME_ID")
    group.add_argument("--live", metavar="GAME_ID")
    ap.add_argument("--speed", type=float, default=10)
    ap.add_argument("--focus", default="NYK")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    source = ReplaySource(args.replay, speed=args.speed) if args.replay else StatsLiveSource(args.live)
    handler = make_handler(source, args.focus, load_baseline(args.focus))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{source.meta.title} {source.meta.away.tricode} @ {source.meta.home.tricode} — {source.mode} — {url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
