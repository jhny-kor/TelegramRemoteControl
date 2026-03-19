from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import tomllib

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from remote_manager import APP_ROOT as REMOTE_APP_ROOT
from remote_manager import build_lifecycle_message
from remote_manager import load_dotenv
from remote_manager import send_message


DEFAULT_CONFIG_PATH = APP_ROOT / "config" / "projects.toml"
DEFAULT_LOG_PATH = APP_ROOT / "logs" / "start_managed_services.log"


@dataclass(frozen=True)
class AutostartProject:
    name: str
    path: Path
    command_key: str
    command: str
    delay_sec: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="remoteBot 부팅 시 관리 대상 서비스를 시작한다.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="프로젝트 설정 TOML 경로",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 실행 없이 시작 대상만 출력",
    )
    return parser.parse_args()


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"[{timestamp()}] {message}\n")


def load_config(config_path: Path) -> dict[str, Any]:
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def notify_startup(
    raw_config: dict[str, Any],
    log_path: Path,
    event: str,
    detail_lines: list[str],
) -> None:
    telegram_raw = raw_config.get("telegram", {})
    bot_token_env = str(telegram_raw.get("bot_token_env", "REMOTE_BOT_TELEGRAM_BOT_TOKEN"))
    chat_ids = [str(chat_id) for chat_id in telegram_raw.get("allowed_chat_ids", [])]

    load_dotenv(REMOTE_APP_ROOT / ".env")
    bot_token = os.getenv(bot_token_env, "").strip()
    if not bot_token or not chat_ids:
        append_log(log_path, f"startup_notification_skipped | event={event}")
        return

    text = build_lifecycle_message(event, detail_lines=detail_lines)
    for chat_id in sorted(set(chat_ids)):
        send_message(bot_token, chat_id, text)
    append_log(log_path, f"startup_notification_sent | event={event}")


def collect_autostart_projects(raw_config: dict[str, Any]) -> list[AutostartProject]:
    projects: list[AutostartProject] = []

    for project_name, project_raw in raw_config.get("projects", {}).items():
        autostart_raw = project_raw.get("autostart", {})
        enabled = bool(autostart_raw.get("enabled", False))
        if not enabled:
            continue

        command_key = str(autostart_raw.get("command_key", "")).strip()
        commands = {
            str(key): str(value)
            for key, value in project_raw.get("commands", {}).items()
        }
        if not command_key:
            raise ValueError(
                f"{project_name} 의 autostart.enabled=true 이지만 command_key 가 비어 있습니다."
            )
        if command_key not in commands:
            raise ValueError(
                f"{project_name} 의 autostart.command_key={command_key!r} 가 commands 에 없습니다."
            )

        projects.append(
            AutostartProject(
                name=str(project_name),
                path=Path(str(project_raw["path"])).expanduser(),
                command_key=command_key,
                command=commands[command_key],
                delay_sec=float(autostart_raw.get("delay_sec", 0)),
            )
        )

    return projects


def run_command(
    command: list[str] | str,
    cwd: Path,
    use_shell: bool,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        shell=use_shell,
        executable="/bin/zsh" if use_shell else None,
        capture_output=True,
        text=True,
        check=False,
    )


def log_completed_process(
    log_path: Path,
    title: str,
    completed: subprocess.CompletedProcess[str],
) -> None:
    append_log(log_path, f"{title} | returncode={completed.returncode}")

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()

    if stdout:
        for line in stdout.splitlines():
            append_log(log_path, f"{title} | stdout | {line}")
    if stderr:
        for line in stderr.splitlines():
            append_log(log_path, f"{title} | stderr | {line}")


def start_remote_manager(config_path: Path, log_path: Path, dry_run: bool) -> None:
    command = [
        sys.executable,
        str(APP_ROOT / "remote_manager.py"),
        "--config",
        str(config_path),
        "--daemon",
    ]
    title = "remote_manager"

    if dry_run:
        append_log(log_path, f"{title} | dry-run | {' '.join(command)}")
        return

    completed = run_command(command, cwd=APP_ROOT, use_shell=False)
    log_completed_process(log_path, title, completed)
    if completed.returncode != 0:
        raise RuntimeError("remote_manager 시작에 실패했습니다.")


def start_project(project: AutostartProject, log_path: Path, dry_run: bool) -> None:
    title = f"{project.name}:{project.command_key}"

    if project.delay_sec > 0:
        append_log(log_path, f"{title} | delay_sec={project.delay_sec}")
        if not dry_run:
            time.sleep(project.delay_sec)

    if dry_run:
        append_log(log_path, f"{title} | dry-run | cwd={project.path} | {project.command}")
        return

    completed = run_command(project.command, cwd=project.path, use_shell=True)
    log_completed_process(log_path, title, completed)
    if completed.returncode != 0:
        raise RuntimeError(f"{project.name} 시작 명령이 실패했습니다.")


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    log_path = DEFAULT_LOG_PATH

    raw_config = load_config(config_path)
    projects = collect_autostart_projects(raw_config)

    append_log(log_path, "=== boot startup begin ===")
    append_log(log_path, f"config={config_path}")
    append_log(log_path, f"dry_run={args.dry_run}")
    append_log(log_path, f"autostart_projects={', '.join(project.name for project in projects) or '-'}")

    try:
        start_remote_manager(config_path, log_path, args.dry_run)
        for project in projects:
            start_project(project, log_path, args.dry_run)
    except Exception as exc:
        append_log(log_path, f"startup_failed | {exc}")
        if not args.dry_run:
            notify_startup(
                raw_config,
                log_path,
                "startup failed",
                detail_lines=[
                    f"오류: {exc}",
                ],
            )
        print(f"시작 실패: {exc}")
        return 1

    if not args.dry_run:
        notify_startup(
            raw_config,
            log_path,
            "startup",
            detail_lines=None,
        )
    append_log(log_path, "=== boot startup end ===")
    print("관리 대상 시작 작업이 완료되었습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
