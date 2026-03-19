#!/bin/zsh
set -euo pipefail

cd /Users/plo/Documents/auto_coin_bot
mkdir -p logs

timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "[$timestamp] boot autostart requested" >> logs/boot_autostart.out

start_bot() {
  local script="$1"
  local stderr_log="logs/${script%.py}.boot.err"
  nohup /Users/plo/Documents/auto_coin_bot/.venv/bin/python "$script" >/dev/null 2>>"$stderr_log" </dev/null &
}

start_bot analysis_log_collector.py
start_bot telegram_command_listener.py
start_bot ma_crossover_bot.py
start_bot upbit_ma_crossover_bot.py
start_bot okx_btc_ema_trend_bot.py
start_bot upbit_btc_ema_trend_bot.py

echo "[$timestamp] boot autostart completed" >> logs/boot_autostart.out
