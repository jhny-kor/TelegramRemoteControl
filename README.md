# TelegramRemoteControl

`TelegramRemoteControl`은 텔레그램에서 여러 로컬 프로젝트를 원격 제어하기 위한 공용 매니저입니다.

핵심 목적은 두 가지입니다.

- 등록된 프로젝트의 운영 명령을 텔레그램에서 안전하게 실행
- 등록된 프로젝트 경로에서 `codex exec`를 호출해 원격 수정 작업 시작

즉, 앞으로 `auto_coin_bot` 말고 다른 프로젝트가 생겨도 이 저장소에 프로젝트만 추가 등록하면 같은 방식으로 제어할 수 있습니다.

## 구조

- `remote_manager.py`: 텔레그램 폴링, 프로젝트 명령 실행, Codex 작업 시작
- `config/projects.toml`: 관리 대상 프로젝트와 허용 명령 정의
- `logs/jobs/<job_id>/`: Codex 작업 로그와 마지막 응답 저장

## 보안 원칙

- 허용된 `chat_id` 에서 온 메시지만 처리합니다.
- 임의 셸 명령은 받지 않습니다.
- 프로젝트별로 `config/projects.toml` 에 등록된 명령만 `/run` 으로 실행합니다.
- `/codex` 도 등록된 프로젝트 경로에서만 실행합니다.

## 빠른 시작

1. `.env.example` 을 참고해 `.env` 파일을 만듭니다.
2. `.env` 안의 `REMOTE_BOT_TELEGRAM_BOT_TOKEN` 에 새 텔레그램 봇 토큰을 넣습니다.
3. `config/projects.toml` 의 `allowed_chat_ids` 를 본인 텔레그램 chat id 로 바꿉니다.
4. 필요하면 `projects.<name>` 항목을 추가해 다른 프로젝트를 등록합니다.
5. 아래 명령으로 설정을 먼저 검증합니다.

```bash
python3 remote_manager.py --once
```

6. 문제가 없으면 실행합니다.

```bash
python3 remote_manager.py
```

설정할 때 주의할 점:

- 실제 봇 토큰은 `.env` 에 넣습니다.
- `config/projects.toml` 의 `bot_token_env` 에는 토큰값이 아니라 환경변수 이름인 `REMOTE_BOT_TELEGRAM_BOT_TOKEN` 이 들어가야 합니다.
- `allowed_chat_ids` 는 이 봇을 사용할 텔레그램 사용자 또는 채팅방 id 목록입니다.
- 예시 파일인 `.env.example` 은 안내용이므로 그대로 실행 파일로 쓰지 않습니다.

터미널을 바로 돌려받고 싶으면 백그라운드로 실행할 수 있습니다.

```bash
python3 remote_manager.py --daemon
python3 remote_manager.py --status
python3 remote_manager.py --stop
```

직접 `nohup` 으로 실행하고 싶다면 아래처럼 써도 됩니다.

```bash
nohup python3 /Users/plo/Documents/remoteBot/remote_manager.py > /Users/plo/Documents/remoteBot/logs/remote_manager.out 2>&1 &
```

## 텔레그램 명령

- `/projects`
- `/ping`
- `/test`
- `/battery`
- `/wifi`
- `/disk`
- `/uptime`
- `/manager`
- `/status <project>`
- `/run <project> <command_key>`
- `/codex <project> <요청>`
- `/jobs`
- `/job <job_id>`
- `/help`

예시:

```text
/projects
/ping
/test
/battery
/wifi
/disk
/uptime
/manager
/status auto_coin_bot
/run auto_coin_bot start_all
/codex auto_coin_bot README에 현재 봇 운영 구조를 반영해 정리해줘
/job auto_coin_bot-20260314-170000
```

## 새 프로젝트 추가

`config/projects.toml` 에 아래 형태로 프로젝트를 추가하면 됩니다.

```toml
[projects.my_project]
path = "/Users/plo/Documents/my_project"
description = "설명"

[projects.my_project.commands]
status = "python3 app.py --status"
restart = "./scripts/restart.sh"

[projects.my_project.codex]
sandbox = "workspace-write"
timeout_sec = 1200
add_dirs = []
skip_git_repo_check = false
```

## 운영 메모

- `codex exec` 는 비동기로 시작되고, 결과는 `logs/jobs/<job_id>/last_message.txt` 에 저장됩니다.
- 텔레그램에서는 `/job <job_id>` 로 마지막 응답과 현재 상태를 확인할 수 있습니다.
- `auto_coin_bot`처럼 별도 텔레그램 리스너가 있는 프로젝트도 이 매니저와 병행할 수 있습니다.
