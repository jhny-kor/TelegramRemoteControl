from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


APP_ROOT = Path("/Users/plo/Documents/remoteBot")
AUTO_COIN_ROOT = Path("/Users/plo/Documents/auto_coin_bot")
AUTO_STOCK_ROOT = Path("/Users/plo/Documents/auto_stock_bot")
HOST = "127.0.0.1"
PORT = 8765
PID_PATH = APP_ROOT / "logs/process_control_server.pid"
SERVER_LOG_PATH = APP_ROOT / "logs/process_control_server.log"
OUT_PATH = APP_ROOT / "logs/process_control_server.out"
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
MAX_LOG_LINES = 120


@dataclass(frozen=True)
class CommandSpec:
    cwd: Path
    argv: list[str]


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    title: str
    subtitle: str
    status_command: CommandSpec
    start_command: CommandSpec
    stop_command: CommandSpec
    expected_running_sections: int


SERVICES = [
    ServiceSpec(
        key="remote_manager",
        title="remote_manager",
        subtitle="텔레그램 원격 제어 매니저",
        status_command=CommandSpec(
            cwd=APP_ROOT,
            argv=[sys.executable, str(APP_ROOT / "remote_manager.py"), "--status"],
        ),
        start_command=CommandSpec(
            cwd=APP_ROOT,
            argv=[sys.executable, str(APP_ROOT / "remote_manager.py"), "--daemon"],
        ),
        stop_command=CommandSpec(
            cwd=APP_ROOT,
            argv=[sys.executable, str(APP_ROOT / "remote_manager.py"), "--stop"],
        ),
        expected_running_sections=1,
    ),
    ServiceSpec(
        key="auto_coin_bot",
        title="auto_coin_bot",
        subtitle="코인 자동매매 6개 프로세스",
        status_command=CommandSpec(
            cwd=AUTO_COIN_ROOT,
            argv=[str(AUTO_COIN_ROOT / ".venv/bin/python"), "bot_manager.py", "status"],
        ),
        start_command=CommandSpec(
            cwd=AUTO_COIN_ROOT,
            argv=[str(AUTO_COIN_ROOT / ".venv/bin/python"), "bot_manager.py", "start", "all"],
        ),
        stop_command=CommandSpec(
            cwd=AUTO_COIN_ROOT,
            argv=[str(AUTO_COIN_ROOT / ".venv/bin/python"), "bot_manager.py", "stop", "all"],
        ),
        expected_running_sections=6,
    ),
    ServiceSpec(
        key="auto_stock_bot",
        title="auto_stock_bot",
        subtitle="한국주식 수집기 프로세스",
        status_command=CommandSpec(
            cwd=AUTO_STOCK_ROOT,
            argv=[str(AUTO_STOCK_ROOT / ".venv/bin/python"), "bot_manager.py", "status"],
        ),
        start_command=CommandSpec(
            cwd=AUTO_STOCK_ROOT,
            argv=[str(AUTO_STOCK_ROOT / ".venv/bin/python"), "bot_manager.py", "start", "all"],
        ),
        stop_command=CommandSpec(
            cwd=AUTO_STOCK_ROOT,
            argv=[str(AUTO_STOCK_ROOT / ".venv/bin/python"), "bot_manager.py", "stop", "all"],
        ),
        expected_running_sections=1,
    ),
]


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.message = ""

    def set_message(self, message: str) -> None:
        with self.lock:
            self.message = message

    def pop_message(self) -> str:
        with self.lock:
            value = self.message
            self.message = ""
            return value


STATE = AppState()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def append_server_log(message: str) -> None:
    SERVER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SERVER_LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write(f"[{now_text()}] {message}\n")


def tail_server_log(limit: int = 30) -> str:
    if not SERVER_LOG_PATH.exists():
        return ""
    lines = SERVER_LOG_PATH.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-limit:])


def run_command(command: CommandSpec, timeout_sec: int = 90) -> tuple[bool, str]:
    completed = subprocess.run(
        command.argv,
        cwd=command.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    output = strip_ansi((completed.stdout or "").strip())
    return completed.returncode == 0, output


def parse_service_state(service: ServiceSpec, output: str) -> str:
    clean = strip_ansi(output)

    if service.key == "remote_manager":
        if "remote_manager 상태: running" in clean:
            return "running"
        if "remote_manager 상태: stopped" in clean:
            return "stopped"
        return "error"

    status_lines = [
        line.strip()
        for line in clean.splitlines()
        if "상태:" in line
    ]
    running_count = sum("실행 중" in line for line in status_lines)
    stopped_count = sum("중지됨" in line for line in status_lines)

    if running_count == service.expected_running_sections:
        return "running"
    if stopped_count == service.expected_running_sections:
        return "stopped"
    if running_count > 0:
        return "partial"
    return "error"


def get_all_statuses() -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for service in SERVICES:
        _, output = run_command(service.status_command)
        detail_lines = output.splitlines()
        detail = "\n".join(detail_lines[:2]) if detail_lines else service.subtitle
        results.append(
            {
                "key": service.key,
                "title": service.title,
                "subtitle": service.subtitle,
                "state": parse_service_state(service, output),
                "detail": detail,
            }
        )
    return results


def apply_desired_state(turn_on: bool) -> str:
    ordered = SERVICES if turn_on else list(reversed(SERVICES))
    action = "시작" if turn_on else "중지"
    for service in ordered:
        command = service.start_command if turn_on else service.stop_command
        success, output = run_command(command)
        append_server_log(f"{service.title} {action} {'성공' if success else '실패'}")
        if output:
            for line in output.splitlines()[:20]:
                append_server_log(f"{service.title} | {line}")
    return f"희망 상태를 {'켜짐' if turn_on else '꺼짐'} 으로 적용했습니다."


def read_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ensure_server_running() -> int:
    pid = read_pid()
    if pid and is_pid_alive(pid):
        return pid

    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("a", encoding="utf-8") as out_stream:
        process = subprocess.Popen(
            [sys.executable, str(APP_ROOT / "scripts/process_control_server.py"), "--serve"],
            cwd=APP_ROOT,
            stdout=out_stream,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    PID_PATH.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def stop_server() -> str:
    pid = read_pid()
    if not pid or not is_pid_alive(pid):
        if PID_PATH.exists():
            PID_PATH.unlink()
        return "제어 서버가 이미 중지되어 있습니다."

    os.kill(pid, signal.SIGTERM)
    return f"제어 서버 종료 신호를 보냈습니다. pid={pid}"


def overall_state_label(statuses: list[dict[str, str]]) -> tuple[str, bool | None]:
    states = [item["state"] for item in statuses]
    if all(state == "running" for state in states):
        return "현재 전체 상태: 모두 켜짐", True
    if all(state == "stopped" for state in states):
        return "현재 전체 상태: 모두 꺼짐", False
    return "현재 전체 상태: 일부만 실행 중", None


def state_badge(state: str) -> tuple[str, str]:
    mapping = {
        "running": ("실행 중", "running"),
        "stopped": ("중지됨", "stopped"),
        "partial": ("일부 실행", "partial"),
        "error": ("확인 필요", "error"),
    }
    return mapping.get(state, ("확인 필요", "error"))


def render_page(message: str = "") -> bytes:
    statuses = get_all_statuses()
    summary_text, desired = overall_state_label(statuses)
    checked = "checked" if desired is not False else ""

    status_cards = []
    for item in statuses:
        badge_text, badge_class = state_badge(item["state"])
        status_cards.append(
            f"""
            <section class="card">
              <div class="row">
                <div>
                  <h3>{html.escape(item['title'])}</h3>
                  <p class="subtitle">{html.escape(item['subtitle'])}</p>
                </div>
                <span class="badge {badge_class}">{html.escape(badge_text)}</span>
              </div>
              <pre>{html.escape(item['detail'])}</pre>
            </section>
            """
        )

    flash = f'<div class="flash">{html.escape(message)}</div>' if message else ""
    log_text = html.escape(tail_server_log())
    page = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Process Control Center</title>
  <style>
    :root {{
      --bg: #efe7dc;
      --card: #fbf8f3;
      --text: #1f1a17;
      --muted: #625b53;
      --line: #ddd2c2;
      --green: #26a65b;
      --green-soft: #dff2e7;
      --gray: #7c7771;
      --gray-soft: #e8e0d7;
      --amber: #946200;
      --amber-soft: #f4e5bb;
      --red: #9f2f2f;
      --red-soft: #f1d9d9;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", sans-serif;
      background: radial-gradient(circle at top left, #f5eee5 0%, var(--bg) 45%, #eadfcf 100%);
      color: var(--text);
    }}
    .wrap {{
      max-width: 920px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }}
    h1 {{
      margin: 0;
      font-size: 34px;
      letter-spacing: -0.03em;
    }}
    .lead {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 15px;
    }}
    .flash {{
      margin-top: 18px;
      padding: 12px 14px;
      border-radius: 14px;
      background: #fff7d1;
      color: #6e5a00;
      font-weight: 600;
    }}
    .panel {{
      margin-top: 20px;
      background: var(--card);
      border-radius: 20px;
      padding: 22px;
      box-shadow: 0 10px 30px rgba(48, 35, 18, 0.08);
    }}
    .top-row {{
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .switch-wrap {{
      display: inline-flex;
      align-items: center;
      gap: 12px;
      font-weight: 700;
    }}
    .switch {{
      position: relative;
      display: inline-block;
      width: 74px;
      height: 40px;
    }}
    .switch input {{
      opacity: 0;
      width: 0;
      height: 0;
    }}
    .slider {{
      position: absolute;
      inset: 0;
      background: #b6b1ab;
      transition: 0.25s;
      border-radius: 999px;
      cursor: pointer;
    }}
    .slider:before {{
      position: absolute;
      content: "";
      height: 30px;
      width: 30px;
      left: 5px;
      top: 5px;
      background: white;
      transition: 0.25s;
      border-radius: 50%;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
    }}
    input:checked + .slider {{
      background: var(--green);
    }}
    input:checked + .slider:before {{
      transform: translateX(34px);
    }}
    .state-text {{
      font-size: 18px;
      color: var(--green);
      min-width: 48px;
    }}
    .summary {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 15px;
    }}
    .actions {{
      margin-top: 18px;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    button, .ghost {{
      border: 0;
      border-radius: 12px;
      padding: 12px 16px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    button {{
      background: #1f7a49;
      color: white;
    }}
    .ghost {{
      background: #e6dccf;
      color: #2f2a25;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 14px;
      margin-top: 18px;
    }}
    .card {{
      background: var(--card);
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 10px 30px rgba(48, 35, 18, 0.08);
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }}
    h3 {{
      margin: 0;
      font-size: 18px;
    }}
    .subtitle {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .badge {{
      white-space: nowrap;
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 12px;
      font-weight: 800;
    }}
    .badge.running {{ background: var(--green-soft); color: var(--green); }}
    .badge.stopped {{ background: var(--gray-soft); color: var(--gray); }}
    .badge.partial {{ background: var(--amber-soft); color: var(--amber); }}
    .badge.error {{ background: var(--red-soft); color: var(--red); }}
    pre {{
      margin: 14px 0 0;
      white-space: pre-wrap;
      font-family: ui-monospace, "SF Mono", monospace;
      background: #f3ede4;
      border-radius: 12px;
      padding: 12px;
      color: #3f3a34;
      min-height: 72px;
    }}
    .log {{
      margin-top: 18px;
      background: #171a1c;
      color: #daf3df;
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 10px 30px rgba(20, 20, 20, 0.25);
    }}
    .log pre {{
      background: transparent;
      color: inherit;
      padding: 0;
      min-height: 120px;
      margin: 0;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Process Control Center</h1>
    <p class="lead">브라우저에서 켜짐/꺼짐 토글을 바꾸고 적용하면 전체 프로세스를 일괄 제어합니다.</p>
    {flash}
    <section class="panel">
      <form method="post" action="/apply">
        <div class="top-row">
          <div class="switch-wrap">
            <span>희망 상태</span>
            <label class="switch">
              <input type="checkbox" id="desiredToggle" name="desired_state" value="on" {checked}>
              <span class="slider"></span>
            </label>
            <span class="state-text" id="desiredStateText">{'켜짐' if checked else '꺼짐'}</span>
          </div>
        </div>
        <div class="summary">{html.escape(summary_text)}</div>
        <div class="actions">
          <button type="submit">적용</button>
          <a class="ghost" href="/">새로고침</a>
        </div>
      </form>
    </section>
    <section class="grid">
      {''.join(status_cards)}
    </section>
    <section class="log">
      <h3 style="margin-top:0;color:white;">실행 로그</h3>
      <pre>{log_text}</pre>
    </section>
  </div>
  <script>
    const toggle = document.getElementById('desiredToggle');
    const text = document.getElementById('desiredStateText');
    const refreshLabel = () => {{
      const on = toggle.checked;
      text.textContent = on ? '켜짐' : '꺼짐';
      text.style.color = on ? '#1f7a49' : '#6b6761';
    }};
    toggle.addEventListener('change', refreshLabel);
    refreshLabel();
  </script>
</body>
</html>
"""
    return page.encode("utf-8")


class ControlHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        message = STATE.pop_message()
        query = parse_qs(parsed.query)
        if not message and "message" in query:
            message = query["message"][0]

        body = render_page(message=message)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/apply":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(raw)
        turn_on = form.get("desired_state", ["off"])[0] == "on"
        append_server_log(f"웹 제어 요청 수신: {'켜짐' if turn_on else '꺼짐'}")
        message = apply_desired_state(turn_on)
        STATE.set_message(message)

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        append_server_log("HTTP " + (format % args))


def serve() -> int:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    append_server_log(f"제어 서버 시작: http://{HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), ControlHandler)
    try:
        server.serve_forever()
    finally:
        if PID_PATH.exists():
            PID_PATH.unlink()
    return 0


def self_test() -> int:
    snapshot = []
    for service in SERVICES:
        _, output = run_command(service.status_command)
        snapshot.append(
            {
                "title": service.title,
                "state": parse_service_state(service, output),
                "detail": output,
            }
        )
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RemoteBot 로컬 제어 서버")
    parser.add_argument("--serve", action="store_true", help="제어 서버 실행")
    parser.add_argument("--ensure-running", action="store_true", help="서버가 없으면 시작")
    parser.add_argument("--open-browser", action="store_true", help="브라우저 열기")
    parser.add_argument("--stop-server", action="store_true", help="제어 서버 종료")
    parser.add_argument("--self-test", action="store_true", help="상태 점검 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.self_test:
        return self_test()

    if args.stop_server:
        print(stop_server())
        return 0

    if args.ensure_running:
        pid = ensure_server_running()
        append_server_log(f"제어 서버 확보 완료: pid={pid}")
        if not args.open_browser and not args.serve:
            print(f"제어 서버 실행 중: pid={pid}")
            return 0

    if args.open_browser:
        subprocess.run(["open", f"http://{HOST}:{PORT}"], check=False)
        return 0

    if args.serve:
        return serve()

    print("사용법: --ensure-running --open-browser 또는 --serve")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
