"""
원격 멀티프로젝트 텔레그램 매니저

- 등록된 프로젝트 목록을 기준으로 상태 조회와 실행 명령을 제한한다.
- 텔레그램에서 /projects, /status, /run, /codex, /jobs, /job 명령을 처리한다.
- /codex 명령은 지정된 프로젝트 경로에서 codex exec 를 비동기 작업으로 실행한다.
- Codex 작업 메타데이터와 로그는 logs/jobs 아래에 저장한다.
- 허용된 chat id 에서 온 메시지만 처리해 임의 접근을 막는다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import tomllib


TELEGRAM_LIMIT = 4000
APP_ROOT = Path(__file__).resolve().parent
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class CodexSettings:
    """프로젝트별 Codex 실행 설정."""

    sandbox: str = "workspace-write"
    timeout_sec: int = 1200
    add_dirs: list[str] = field(default_factory=list)
    model: str | None = None
    skip_git_repo_check: bool = False


@dataclass(frozen=True)
class ProjectConfig:
    """관리 대상 프로젝트 설정."""

    name: str
    path: Path
    description: str
    managed_programs: dict[str, str]
    commands: dict[str, str]
    codex: CodexSettings


@dataclass(frozen=True)
class TelegramSettings:
    """텔레그램 연동 설정."""

    bot_token_env: str
    allowed_chat_ids: set[str]
    poll_interval_sec: int
    offset_path: Path


@dataclass(frozen=True)
class ManagerSettings:
    """매니저 공통 설정."""

    codex_bin: str
    jobs_dir: Path
    config_path: Path
    pid_path: Path
    manager_log_path: Path


@dataclass
class AppConfig:
    """애플리케이션 전체 설정."""

    telegram: TelegramSettings
    manager: ManagerSettings
    projects: dict[str, ProjectConfig]


@dataclass
class JobRecord:
    """Codex 작업 메타데이터."""

    job_id: str
    project: str
    instruction: str
    pid: int
    started_at: str
    status: str
    log_path: str
    last_message_path: str
    command: list[str]
    timeout_sec: int
    finished_at: str | None = None


def load_dotenv(dotenv_path: Path) -> None:
    """간단한 .env 파일을 읽어 비어 있는 환경 변수만 채운다."""
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_app_path(raw_path: str | Path) -> Path:
    """앱 루트 기준 상대 경로를 절대 경로로 바꾼다."""
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (APP_ROOT / path).resolve()


def load_config(config_path: Path) -> AppConfig:
    """TOML 설정 파일을 읽어 구조화된 설정 객체로 변환한다."""
    config_path = resolve_app_path(config_path)
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))

    telegram_raw = raw.get("telegram", {})
    manager_raw = raw.get("manager", {})
    projects_raw = raw.get("projects", {})

    if not projects_raw:
        raise ValueError("config/projects.toml 에 프로젝트가 하나 이상 필요합니다.")

    offset_path = resolve_app_path(
        telegram_raw.get("offset_path", "logs/telegram_manager.offset")
    )
    telegram = TelegramSettings(
        bot_token_env=str(telegram_raw.get("bot_token_env", "REMOTE_BOT_TELEGRAM_BOT_TOKEN")),
        allowed_chat_ids={str(chat_id) for chat_id in telegram_raw.get("allowed_chat_ids", [])},
        poll_interval_sec=int(telegram_raw.get("poll_interval_sec", 5)),
        offset_path=offset_path,
    )

    jobs_dir = resolve_app_path(manager_raw.get("jobs_dir", "logs/jobs"))
    manager = ManagerSettings(
        codex_bin=str(
            manager_raw.get(
                "codex_bin",
                "/Applications/Codex.app/Contents/Resources/codex",
            )
        ),
        jobs_dir=jobs_dir,
        config_path=config_path,
        pid_path=resolve_app_path(manager_raw.get("pid_path", "logs/remote_manager.pid")),
        manager_log_path=resolve_app_path(
            manager_raw.get("manager_log_path", "logs/remote_manager.out")
        ),
    )

    projects: dict[str, ProjectConfig] = {}
    for name, project_raw in projects_raw.items():
        codex_raw = project_raw.get("codex", {})
        project = ProjectConfig(
            name=name,
            path=Path(project_raw["path"]).expanduser(),
            description=str(project_raw.get("description", "")),
            managed_programs={
                str(k): str(v) for k, v in project_raw.get("managed_programs", {}).items()
            },
            commands={str(k): str(v) for k, v in project_raw.get("commands", {}).items()},
            codex=CodexSettings(
                sandbox=str(codex_raw.get("sandbox", "workspace-write")),
                timeout_sec=int(codex_raw.get("timeout_sec", 1200)),
                add_dirs=[str(item) for item in codex_raw.get("add_dirs", [])],
                model=(
                    str(codex_raw["model"])
                    if codex_raw.get("model") not in (None, "")
                    else None
                ),
                skip_git_repo_check=bool(codex_raw.get("skip_git_repo_check", False)),
            ),
        )
        projects[name] = project

    return AppConfig(telegram=telegram, manager=manager, projects=projects)


def telegram_api_request(
    bot_token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any] | None:
    """텔레그램 Bot API 를 호출한다."""
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = None
    headers: dict[str, str] = {}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url=url,
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None


def get_updates(
    bot_token: str,
    offset: int,
    timeout: int = 20,
) -> tuple[list[dict[str, Any]], str | None]:
    """새 텔레그램 업데이트 목록을 가져온다."""
    query = urllib.parse.urlencode({"offset": offset, "timeout": timeout})
    result = telegram_api_request(
        bot_token,
        f"getUpdates?{query}",
        payload=None,
        timeout=timeout + 5,
    )
    if not result:
        return [], "Telegram API 요청이 실패했습니다."
    if not result.get("ok"):
        description = result.get("description")
        if isinstance(description, str) and description.strip():
            return [], f"Telegram API 오류: {description.strip()}"
        return [], "Telegram API 가 getUpdates 요청을 거부했습니다."
    return result.get("result", []), None


def append_manager_log(config: AppConfig, message: str) -> None:
    """매니저 운영 로그를 파일 끝에 추가한다."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    config.manager.manager_log_path.parent.mkdir(parents=True, exist_ok=True)
    with config.manager.manager_log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"[{timestamp}] {message}\n")


def send_message(bot_token: str, chat_id: str, text: str) -> None:
    """텔레그램 메시지를 길이에 맞춰 나눠 전송한다."""
    chunks = chunk_text(text, TELEGRAM_LIMIT)
    for chunk in chunks:
        telegram_api_request(
            bot_token,
            "sendMessage",
            payload={"chat_id": chat_id, "text": chunk},
            timeout=30,
        )


def send_broadcast_message(bot_token: str, chat_ids: set[str], text: str) -> None:
    """허용된 모든 chat_id 로 같은 메시지를 전송한다."""
    for chat_id in sorted(chat_ids):
        send_message(bot_token, chat_id, text)


def load_bot_token(config: AppConfig) -> str:
    """환경 변수에서 텔레그램 봇 토큰을 읽는다."""
    load_dotenv(APP_ROOT / ".env")
    return os.getenv(config.telegram.bot_token_env, "").strip()


def build_lifecycle_message(event: str, detail_lines: list[str] | None = None) -> str:
    """매니저 라이프사이클 알림 메시지를 만든다."""
    event_titles = {
        "startup": "전원 켜짐",
        "shutdown": "전원 꺼짐",
        "startup failed": "전원 켜짐 실패",
    }
    title = event_titles.get(event, event)
    lines = [
        title,
        f"시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if detail_lines:
        lines.extend(detail_lines)
    return "\n".join(lines)


def notify_manager_lifecycle(
    config: AppConfig,
    bot_token: str,
    event: str,
    detail_lines: list[str] | None = None,
) -> None:
    """매니저 시작/종료 알림을 텔레그램으로 전송한다."""
    if not bot_token or not config.telegram.allowed_chat_ids:
        return

    text = build_lifecycle_message(event, detail_lines=detail_lines)
    send_broadcast_message(bot_token, config.telegram.allowed_chat_ids, text)
    append_manager_log(config, f"lifecycle notification sent: event={event}")


def chunk_text(text: str, limit: int) -> list[str]:
    """긴 메시지를 줄 단위 중심으로 분할한다."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for line in text.splitlines():
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            for start in range(0, len(line), limit):
                chunks.append(line[start : start + limit])
            continue
        extra = len(line) + (1 if current else 0)
        if current and current_length + extra > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_length = len(line)
            continue
        current.append(line)
        current_length += extra

    if current:
        chunks.append("\n".join(current))
    return chunks


def load_offset(path: Path) -> int:
    """마지막 offset 을 읽는다."""
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0


def save_offset(path: Path, offset: int) -> None:
    """마지막 offset 을 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(offset), encoding="utf-8")


def load_pid(pid_path: Path) -> int | None:
    """pid 파일에서 프로세스 id 를 읽는다."""
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def save_pid(pid_path: Path, pid: int) -> None:
    """pid 파일을 저장한다."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def clear_pid(pid_path: Path) -> None:
    """pid 파일을 삭제한다."""
    if pid_path.exists():
        pid_path.unlink()


def normalize_command(text: str) -> str:
    """명령 토큰만 정규화한다."""
    first = text.strip().split()[0].lower()
    if "@" in first:
        first = first.split("@", 1)[0]
    return first if first.startswith("/") else f"/{first}"


def build_help_text(config: AppConfig) -> str:
    """지원 명령 설명을 만든다."""
    project_names = ", ".join(sorted(config.projects)) or "-"
    return (
        "사용 가능한 명령\n"
        "- /ping : 간단한 응답 확인\n"
        "- /test : 매니저 기본 설정 점검\n"
        "- /battery : 현재 맥북 배터리 상태 확인\n"
        "- /wifi : 현재 맥북 Wi-Fi 상태 확인\n"
        "- /disk : 현재 시스템 디스크 사용량 확인\n"
        "- /uptime : 현재 시스템 가동 시간과 부하 확인\n"
        f"- /projects : 등록된 프로젝트 목록 확인 ({project_names})\n"
        "- /manager : remoteBot 매니저 상태 확인\n"
        "- /status <project> : 등록된 status 명령 실행\n"
        "- /run <project> <command_key> : 등록된 실행 명령 호출\n"
        "- /codex <project> <요청> : 해당 프로젝트에서 Codex 작업 시작\n"
        "- /jobs : 최근 Codex 작업 목록\n"
        "- /job <job_id> : 작업 상세 상태와 마지막 응답 확인\n"
        "- /help : 도움말"
    )


def format_projects(config: AppConfig) -> str:
    """등록된 프로젝트 목록을 문자열로 만든다."""
    lines = ["등록된 프로젝트"]
    for project in sorted(config.projects.values(), key=lambda item: item.name):
        command_keys = ", ".join(sorted(project.commands)) or "-"
        description = f" - {project.description}" if project.description else ""
        lines.append(f"- {project.name}{description}")
        lines.append(f"  path: {project.path}")
        if project.managed_programs:
            lines.append("  managed_programs:")
            for program_name, program_description in sorted(project.managed_programs.items()):
                lines.append(f"  - {program_name}: {program_description}")
        lines.append(f"  commands: {command_keys}")
    return "\n".join(lines)


def build_ping_text() -> str:
    """간단한 생존 응답 메시지를 만든다."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"pong\nserver_time: {now}"


def build_test_text(config: AppConfig) -> str:
    """기본 설정 점검 메시지를 만든다."""
    project_count = len(config.projects)
    project_names = ", ".join(sorted(config.projects)) or "-"
    bot_token_loaded = "yes" if os.getenv(config.telegram.bot_token_env, "").strip() else "no"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "remoteBot test ok\n"
        f"server_time: {now}\n"
        f"bot_token_loaded: {bot_token_loaded}\n"
        f"allowed_chat_ids: {len(config.telegram.allowed_chat_ids)}\n"
        f"projects: {project_count}\n"
        f"project_names: {project_names}"
    )


def build_battery_text() -> str:
    """현재 맥북 배터리 상태를 읽어 사람이 보기 쉬운 문자열로 만든다."""
    try:
        completed = subprocess.run(
            ["pmset", "-g", "batt"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "배터리 상태 명령을 찾지 못했습니다. 이 환경에서는 pmset 을 사용할 수 없습니다."
    except subprocess.TimeoutExpired:
        return "배터리 상태 조회 시간이 초과되었습니다."
    except OSError as exc:
        return f"배터리 상태 조회에 실패했습니다: {exc}"

    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        return "배터리 상태를 읽지 못했습니다."

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) >= 2:
        power_source = lines[0].replace("Now drawing from ", "").strip().strip("'")
        detail = lines[1].split("\t")[-1].strip()
        parts = [part.strip() for part in detail.split(";") if part.strip()]
        level = parts[0] if parts else "-"
        raw_status = parts[1].lower() if len(parts) > 1 else ""
        raw_time = parts[2] if len(parts) > 2 else ""

        source_label = "전원 어댑터 연결" if power_source == "AC Power" else "배터리 사용 중"
        status_map = {
            "charging": "충전 중",
            "discharging": "사용 중",
            "charged": "충전 완료",
            "finishing charge": "충전 마무리 중",
        }
        status_label = status_map.get(raw_status, raw_status or "-")
        time_label = raw_time.replace(" remaining", "").strip() if raw_time else "-"
        time_label = time_label.split(" present:", 1)[0].strip()
        if ":" in time_label:
            hour_text, minute_text = time_label.split(":", 1)
            if hour_text.isdigit() and minute_text.isdigit():
                time_label = f"{int(hour_text)}시간 {minute_text.zfill(2)}분"
        return (
            "배터리 상태\n"
            f"전원 공급: {source_label}\n"
            f"배터리 잔량: {level}\n"
            f"상태: {status_label}\n"
            f"예상 시간: {time_label}"
        )

    return f"배터리 상태\n{output}"


def build_wifi_text() -> str:
    """현재 맥북 Wi-Fi 상태를 읽어 요약 문자열로 만든다."""
    try:
        completed = subprocess.run(
            ["system_profiler", "SPAirPortDataType"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        return "Wi-Fi 상태 명령을 찾지 못했습니다. 이 환경에서는 system_profiler 를 사용할 수 없습니다."
    except subprocess.TimeoutExpired:
        return "Wi-Fi 상태 조회 시간이 초과되었습니다."
    except OSError as exc:
        return f"Wi-Fi 상태 조회에 실패했습니다: {exc}"

    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        return "Wi-Fi 상태를 읽지 못했습니다."

    interface = "-"
    status = "-"
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("en") and line.endswith(":"):
            interface = line[:-1]
        elif line.startswith("Status:"):
            status = line.split(":", 1)[1].strip()
            break

    return (
        "wifi status\n"
        f"interface: {interface}\n"
        f"status: {status}"
    )


def kib_to_human(size_kib: int) -> str:
    """KiB 기준 크기를 읽기 쉬운 KB/MB/GB 문자열로 바꾼다."""
    size = float(size_kib)
    units = ["KB", "MB", "GB", "TB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def collect_documents_directory_usage(
    documents_dir: Path,
    limit: int = 12,
) -> tuple[str, list[str]] | tuple[None, None]:
    """Documents 총 사용량과 상위 디렉터리 사용량 목록을 만든다."""
    if not documents_dir.exists():
        return None, None

    directories = sorted(
        [path for path in documents_dir.iterdir() if path.is_dir()],
        key=lambda item: item.name.lower(),
    )
    if not directories:
        return "0 KB", []

    try:
        total_completed = subprocess.run(
            ["du", "-sk", str(documents_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        entries_completed = subprocess.run(
            ["du", "-sk", *[str(path) for path in directories]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return None, None
    except subprocess.TimeoutExpired:
        return "timeout", []
    except OSError:
        return None, None

    if total_completed.returncode != 0 or entries_completed.returncode != 0:
        return None, None

    total_raw = (total_completed.stdout or "").strip().splitlines()
    total_usage = None
    if total_raw:
        total_parts = total_raw[0].split("\t", 1)
        if total_parts and total_parts[0].isdigit():
            total_usage = kib_to_human(int(total_parts[0]))

    usages: list[tuple[int, str]] = []
    for raw_line in (entries_completed.stdout or "").splitlines():
        parts = raw_line.split("\t", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        path = Path(parts[1].strip())
        usages.append((int(parts[0]), path.name))

    usages.sort(key=lambda item: item[0], reverse=True)
    lines = [f"- {name}: {kib_to_human(size_kib)}" for size_kib, name in usages[:limit]]
    remaining_count = max(0, len(usages) - limit)
    if remaining_count:
        lines.append(f"- 기타 {remaining_count}개 디렉터리: 생략")

    return total_usage, lines


def build_disk_text() -> str:
    """현재 시스템 디스크와 Documents 사용량을 함께 요약한다."""
    try:
        stats = os.statvfs("/")
    except OSError as exc:
        return f"디스크 상태 조회에 실패했습니다: {exc}"

    block_size = stats.f_frsize or stats.f_bsize or 1024
    total_kib = int((stats.f_blocks * block_size) / 1024)
    available_kib = int((stats.f_bavail * block_size) / 1024)
    used_kib = max(0, total_kib - available_kib)
    capacity = f"{int((used_kib / total_kib) * 100) if total_kib else 0}%"

    size = kib_to_human(total_kib)
    used = kib_to_human(used_kib)
    avail = kib_to_human(available_kib)

    lines = [
        "디스크 상태",
        f"맥 전체 용량: {size}",
        f"맥 잔여 용량: {avail}",
        f"맥 사용량: {used}",
        f"사용 비율: {capacity}",
    ]

    documents_dir = Path.home() / "Documents"
    documents_total, documents_lines = collect_documents_directory_usage(documents_dir)
    if documents_total is not None:
        lines.append("")
        lines.append(f"문서 폴더 경로: {documents_dir}")
        lines.append(f"문서 폴더 전체 사용량: {documents_total}")
        lines.append("문서 폴더별 사용량:")
        if documents_lines:
            lines.extend(documents_lines)
        else:
            lines.append("- 디렉터리가 없습니다.")

    return "\n".join(lines)


def format_duration_korean(total_seconds: int) -> str:
    """초 단위 시간을 한국어 기간 문자열로 바꾼다."""
    minutes, _ = divmod(max(0, total_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    parts: list[str] = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    if minutes or not parts:
        parts.append(f"{minutes}분")
    return " ".join(parts)


def parse_boot_time() -> datetime | None:
    """who -b 결과를 현재 연도 기준 부팅 시각으로 해석한다."""
    try:
        completed = subprocess.run(
            ["who", "-b"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        return None

    normalized = " ".join(output.split())
    if "system boot" not in normalized:
        return None

    raw_value = normalized.split("system boot", 1)[1].strip()
    now = datetime.now()

    for year in (now.year, now.year - 1):
        try:
            candidate = datetime.strptime(f"{year} {raw_value}", "%Y %b %d %H:%M")
        except ValueError:
            continue
        if candidate <= now:
            return candidate

    return None


def interpret_load_average(load_1m: float) -> str:
    """1분 평균 부하값을 읽기 쉬운 한글 상태로 해석한다."""
    if load_1m < 1.5:
        return "낮음"
    if load_1m < 3.0:
        return "보통"
    if load_1m < 6.0:
        return "높음"
    return "매우 높음"


def build_uptime_text() -> str:
    """현재 시스템 uptime 정보를 읽기 쉬운 한글 문자열로 만든다."""
    try:
        completed = subprocess.run(
            ["uptime"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "uptime 명령을 찾지 못했습니다."
    except subprocess.TimeoutExpired:
        return "uptime 조회 시간이 초과되었습니다."
    except OSError as exc:
        return f"uptime 조회에 실패했습니다: {exc}"

    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        return "uptime 정보를 읽지 못했습니다."

    normalized = " ".join(output.split())
    users_match = re.search(r"(\d+)\s+users?", normalized)
    load_match = re.search(
        r"load averages:\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)",
        normalized,
    )

    now = datetime.now()
    boot_time = parse_boot_time()

    lines = [
        "시스템 가동 상태",
        f"현재 시각: {now.strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    if boot_time is not None:
        uptime_seconds = int((now - boot_time).total_seconds())
        lines.append(f"부팅 시각: {boot_time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"가동 시간: {format_duration_korean(uptime_seconds)}")

    if users_match:
        lines.append(f"현재 로그인 세션 수: {users_match.group(1)}개")

    if load_match:
        load_1m = float(load_match.group(1))
        lines.append(
            "시스템 부하(1분/5분/15분): "
            f"{load_match.group(1)} / {load_match.group(2)} / {load_match.group(3)}"
        )
        lines.append(f"부하 해석: {interpret_load_average(load_1m)}")
    else:
        lines.append(f"원본 uptime: {normalized}")

    return "\n".join(lines)


def strip_ansi_escape_sequences(text: str) -> str:
    """터미널 ANSI escape sequence 를 제거한다."""
    return ANSI_ESCAPE_RE.sub("", text)


def run_registered_command(
    project: ProjectConfig,
    command_key: str,
    timeout_sec: int = 120,
) -> tuple[bool, str]:
    """등록된 명령만 실행한다."""
    command = project.commands.get(command_key)
    if not command:
        return False, f"등록되지 않은 명령입니다: {command_key}"

    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=project.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return False, f"명령 시간이 초과되었습니다. ({timeout_sec}초)"
    except OSError as exc:
        return False, f"명령 실행에 실패했습니다: {exc}"

    output = strip_ansi_escape_sequences((completed.stdout or "")).strip()
    if not output:
        output = "(출력 없음)"

    prefix = "성공" if completed.returncode == 0 else f"실패(rc={completed.returncode})"
    return completed.returncode == 0, f"{prefix}\n{output[-3000:]}"


def build_codex_prompt(project: ProjectConfig, instruction: str) -> str:
    """프로젝트별 Codex 기본 프롬프트를 만든다."""
    return (
        f"작업 루트는 {project.path} 입니다.\n"
        "저장소 안의 AGENTS.md 가 있으면 반드시 따르고, 관련 파일만 최소 범위로 수정하세요.\n"
        "필요한 변경을 실제로 적용하고, 마지막에는 무엇을 바꿨는지 간단히 요약하세요.\n\n"
        f"사용자 요청:\n{instruction.strip()}"
    )


def launch_codex_job(config: AppConfig, project: ProjectConfig, instruction: str) -> JobRecord:
    """Codex 비동기 작업을 시작한다."""
    config.manager.jobs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_id = f"{project.name}-{timestamp}"
    job_dir = config.manager.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    log_path = job_dir / "codex.log"
    last_message_path = job_dir / "last_message.txt"
    metadata_path = job_dir / "job.json"
    prompt = build_codex_prompt(project, instruction)

    command = [
        config.manager.codex_bin,
        "exec",
        "--full-auto",
        "-C",
        str(project.path),
        "-s",
        project.codex.sandbox,
        "-o",
        str(last_message_path),
    ]

    if project.codex.model:
        command.extend(["-m", project.codex.model])
    if project.codex.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    for add_dir in project.codex.add_dirs:
        command.extend(["--add-dir", add_dir])
    command.append(prompt)

    with log_path.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command,
            cwd=project.path,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    job = JobRecord(
        job_id=job_id,
        project=project.name,
        instruction=instruction.strip(),
        pid=process.pid,
        started_at=datetime.now().isoformat(timespec="seconds"),
        status="running",
        log_path=str(log_path),
        last_message_path=str(last_message_path),
        command=command,
        timeout_sec=project.codex.timeout_sec,
    )
    metadata_path.write_text(
        json.dumps(asdict(job), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return job


def load_job(job_path: Path) -> JobRecord:
    """작업 메타데이터를 읽는다."""
    raw = json.loads(job_path.read_text(encoding="utf-8"))
    return JobRecord(**raw)


def save_job(job_path: Path, job: JobRecord) -> None:
    """작업 메타데이터를 저장한다."""
    job_path.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")


def is_process_running(pid: int) -> bool:
    """pid 가 아직 살아 있는지 확인한다."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def get_manager_pid(config: AppConfig) -> int | None:
    """매니저 pid 파일과 실제 프로세스 상태를 함께 확인한다."""
    pid = load_pid(config.manager.pid_path)
    if pid is None:
        return None
    if is_process_running(pid):
        return pid
    clear_pid(config.manager.pid_path)
    return None


def format_manager_status(config: AppConfig) -> str:
    """매니저 자체의 실행 상태를 문자열로 반환한다."""
    pid = get_manager_pid(config)
    if pid is None:
        return "remote_manager 상태: stopped"

    lines = [
        "remote_manager 상태: running",
        f"pid: {pid}",
        f"log: {config.manager.manager_log_path}",
    ]
    log_tail = tail_text(config.manager.manager_log_path, 20)
    if log_tail:
        lines.append("")
        lines.append("[log_tail]")
        lines.append(log_tail[-2000:])
    return "\n".join(lines)


def launch_daemon(config: AppConfig) -> str:
    """매니저를 백그라운드 프로세스로 시작한다."""
    existing_pid = get_manager_pid(config)
    if existing_pid is not None:
        return f"이미 실행 중입니다. pid={existing_pid}"

    config.manager.manager_log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(APP_ROOT / "remote_manager.py"),
        "--config",
        str(config.manager.config_path),
    ]

    with config.manager.manager_log_path.open("a", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command,
            cwd=APP_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )

    save_pid(config.manager.pid_path, process.pid)
    return (
        "remote_manager를 백그라운드로 시작했습니다.\n"
        f"pid: {process.pid}\n"
        f"log: {config.manager.manager_log_path}\n"
        "확인: python3 remote_manager.py --status"
    )


def stop_daemon(config: AppConfig) -> str:
    """백그라운드 매니저를 종료한다."""
    pid = get_manager_pid(config)
    if pid is None:
        return "중지할 remote_manager 프로세스가 없습니다."

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_pid(config.manager.pid_path)
        return "이미 종료된 프로세스였습니다."
    except PermissionError:
        return (
            "종료 권한이 없습니다. 같은 사용자 셸에서 직접 종료하거나 "
            f"`kill -TERM -{pid}` 를 실행하세요."
        )

    time.sleep(0.5)
    if is_process_running(pid):
        return f"종료 신호를 보냈지만 아직 실행 중입니다. pid={pid}"

    clear_pid(config.manager.pid_path)
    return f"remote_manager를 종료했습니다. pid={pid}"


def refresh_job_status(job_path: Path) -> JobRecord:
    """실행 중 작업 상태를 현재 기준으로 갱신한다."""
    job = load_job(job_path)
    if job.status in {"completed", "failed", "timed_out", "timed_out_no_permission"}:
        return job

    started_at = datetime.fromisoformat(job.started_at)
    elapsed_sec = (datetime.now() - started_at).total_seconds()
    if elapsed_sec > job.timeout_sec and is_process_running(job.pid):
        try:
            os.killpg(job.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            job.status = "timed_out_no_permission"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            save_job(job_path, job)
            return job
        job.status = "timed_out"
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        save_job(job_path, job)
        return job

    if is_process_running(job.pid):
        return job

    last_message = Path(job.last_message_path)
    status = "completed" if last_message.exists() and last_message.read_text(encoding="utf-8").strip() else "failed"
    job.status = status
    job.finished_at = datetime.now().isoformat(timespec="seconds")
    save_job(job_path, job)
    return job


def list_job_paths(jobs_dir: Path) -> list[Path]:
    """작업 메타데이터 파일 목록을 최신순으로 반환한다."""
    if not jobs_dir.exists():
        return []
    return sorted(jobs_dir.glob("*/job.json"), reverse=True)


def format_jobs(jobs_dir: Path, limit: int = 10) -> str:
    """최근 작업 목록을 포맷한다."""
    job_paths = list_job_paths(jobs_dir)[:limit]
    if not job_paths:
        return "최근 Codex 작업이 없습니다."

    lines = ["최근 Codex 작업"]
    for job_path in job_paths:
        job = refresh_job_status(job_path)
        lines.append(
            f"- {job.job_id} | {job.project} | {job.status} | {job.started_at}"
        )
    return "\n".join(lines)


def format_job_detail(jobs_dir: Path, job_id: str) -> str:
    """단일 작업 상세 정보를 포맷한다."""
    job_path = jobs_dir / job_id / "job.json"
    if not job_path.exists():
        return f"작업을 찾지 못했습니다: {job_id}"

    job = refresh_job_status(job_path)
    lines = [
        f"job_id: {job.job_id}",
        f"project: {job.project}",
        f"status: {job.status}",
        f"started_at: {job.started_at}",
    ]
    if job.finished_at:
        lines.append(f"finished_at: {job.finished_at}")
    lines.append(f"instruction: {job.instruction}")

    last_message_path = Path(job.last_message_path)
    if last_message_path.exists():
        message = last_message_path.read_text(encoding="utf-8").strip()
        if message:
            lines.append("")
            lines.append("[last_message]")
            lines.append(message[-2500:])

    log_path = Path(job.log_path)
    if log_path.exists() and job.status != "completed":
        log_tail = tail_text(log_path, 40)
        if log_tail:
            lines.append("")
            lines.append("[log_tail]")
            lines.append(log_tail[-2000:])

    return "\n".join(lines)


def tail_text(path: Path, line_count: int) -> str:
    """텍스트 파일 마지막 일부를 읽는다."""
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[-line_count:])


def parse_message_text(update: dict[str, Any]) -> tuple[str | None, str | None]:
    """업데이트에서 chat id 와 텍스트를 꺼낸다."""
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text")
    if chat_id is None or not text:
        return None, None
    return str(chat_id), str(text)


def handle_command(
    config: AppConfig,
    text: str,
) -> str:
    """명령 텍스트를 해석해 응답 문자열을 만든다."""
    parts = text.strip().split(maxsplit=2)
    if not parts:
        return build_help_text(config)

    command = normalize_command(parts[0])

    if command in {"/help", "/start"}:
        return build_help_text(config)

    if command == "/ping":
        return build_ping_text()

    if command == "/test":
        return build_test_text(config)

    if command == "/battery":
        return build_battery_text()

    if command == "/wifi":
        return build_wifi_text()

    if command == "/disk":
        return build_disk_text()

    if command == "/uptime":
        return build_uptime_text()

    if command == "/projects":
        return format_projects(config)

    if command == "/manager":
        return format_manager_status(config)

    if command == "/jobs":
        return format_jobs(config.manager.jobs_dir)

    if command == "/job":
        if len(parts) < 2:
            return "사용법: /job <job_id>"
        return format_job_detail(config.manager.jobs_dir, parts[1])

    if command in {"/status", "/run", "/codex"}:
        if len(parts) < 2:
            return f"사용법: {command} <project> ..."
        project_name = parts[1]
        project = config.projects.get(project_name)
        if not project:
            return f"등록되지 않은 프로젝트입니다: {project_name}"
        if not project.path.exists():
            return f"프로젝트 경로가 없습니다: {project.path}"

        if command == "/status":
            status_key = "status"
            ok, result = run_registered_command(project, status_key)
            title = f"[{project.name}] status"
            return f"{title}\n{result}" if ok else f"{title}\n{result}"

        if command == "/run":
            if len(parts) < 3:
                return "사용법: /run <project> <command_key>"
            command_key = parts[2].strip()
            ok, result = run_registered_command(project, command_key)
            title = f"[{project.name}] run {command_key}"
            return f"{title}\n{result}" if ok else f"{title}\n{result}"

        if command == "/codex":
            if len(parts) < 3:
                return "사용법: /codex <project> <요청>"
            instruction = parts[2].strip()
            if not instruction:
                return "Codex 요청 내용이 비어 있습니다."
            job = launch_codex_job(config, project, instruction)
            return (
                f"[{project.name}] Codex 작업을 시작했습니다.\n"
                f"job_id: {job.job_id}\n"
                f"확인: /job {job.job_id}"
            )

    return build_help_text(config)


def run_polling(config: AppConfig) -> None:
    """텔레그램 폴링 루프를 실행한다."""
    bot_token = load_bot_token(config)
    if not bot_token:
        raise ValueError(
            f"텔레그램 봇 토큰이 없습니다. 환경 변수 {config.telegram.bot_token_env} 를 설정하세요."
        )
    if not config.telegram.allowed_chat_ids:
        raise ValueError("config/projects.toml 의 allowed_chat_ids 를 하나 이상 설정하세요.")

    offset = load_offset(config.telegram.offset_path)
    append_manager_log(
        config,
        f"remote_manager polling started. offset={offset} allowed_chat_ids={sorted(config.telegram.allowed_chat_ids)}",
    )
    while True:
        updates, error_message = get_updates(bot_token, offset=offset, timeout=20)
        if error_message:
            append_manager_log(config, error_message)
            time.sleep(config.telegram.poll_interval_sec)
            continue

        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            chat_id, text = parse_message_text(update)
            if not chat_id or not text:
                continue
            if chat_id not in config.telegram.allowed_chat_ids:
                append_manager_log(
                    config,
                    f"ignored command from unauthorized chat_id={chat_id} text={text!r}",
                )
                continue

            append_manager_log(config, f"received command chat_id={chat_id} text={text!r}")
            response = handle_command(config, text)
            send_message(bot_token, chat_id, response)
            save_offset(config.telegram.offset_path, offset)
            append_manager_log(config, f"handled command chat_id={chat_id} next_offset={offset}")

        time.sleep(config.telegram.poll_interval_sec)


def build_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 만든다."""
    parser = argparse.ArgumentParser(description="텔레그램 기반 멀티프로젝트 원격 매니저")
    parser.add_argument(
        "--config",
        default="config/projects.toml",
        help="프로젝트 설정 TOML 경로",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="설정만 검증하고 종료",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="매니저를 백그라운드로 시작하고 즉시 종료",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="백그라운드 매니저를 종료",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="백그라운드 매니저 상태 확인",
    )
    return parser


def main() -> None:
    """프로그램 진입점."""
    parser = build_parser()
    args = parser.parse_args()

    config_path = resolve_app_path(args.config)
    config = load_config(config_path)

    config.telegram.offset_path.parent.mkdir(parents=True, exist_ok=True)
    config.manager.jobs_dir.mkdir(parents=True, exist_ok=True)
    config.manager.pid_path.parent.mkdir(parents=True, exist_ok=True)
    config.manager.manager_log_path.parent.mkdir(parents=True, exist_ok=True)

    if args.once:
        print("설정 검증 완료")
        print(format_projects(config))
        return

    if args.daemon:
        print(launch_daemon(config))
        return

    if args.stop:
        print(stop_daemon(config))
        return

    if args.status:
        print(format_manager_status(config))
        return

    bot_token = load_bot_token(config)
    shutdown_reason = "normal_exit"

    def handle_shutdown_signal(signum: int, _frame: Any) -> None:
        nonlocal shutdown_reason
        signal_name = signal.Signals(signum).name
        shutdown_reason = signal_name
        append_manager_log(config, f"shutdown signal received: {signal_name}")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    save_pid(config.manager.pid_path, os.getpid())
    try:
        run_polling(config)
    except KeyboardInterrupt:
        shutdown_reason = "KeyboardInterrupt"
    except SystemExit:
        raise
    except Exception as exc:
        shutdown_reason = f"error:{exc.__class__.__name__}"
        raise
    finally:
        current_pid = load_pid(config.manager.pid_path)
        if current_pid == os.getpid():
            clear_pid(config.manager.pid_path)
        try:
            notify_manager_lifecycle(
                config,
                bot_token,
                "shutdown",
                detail_lines=None,
            )
        except Exception as exc:
            append_manager_log(config, f"lifecycle notification failed: event=shutdown error={exc}")


if __name__ == "__main__":
    main()
