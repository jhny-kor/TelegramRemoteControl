#!/bin/zsh
set -euo pipefail

cd /Users/plo/Documents/remoteBot
exec /opt/homebrew/bin/python3 /Users/plo/Documents/remoteBot/scripts/process_control_server.py --ensure-running --open-browser
