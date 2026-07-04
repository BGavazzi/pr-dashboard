#!/usr/bin/env bash
# Thin wrapper so UTF-8 + PYTHONUNBUFFERED are set before launching the dashboard.
# Usage mirrors pr-dash.ps1:
#   ./pr-dash.sh             # watch 60s
#   ./pr-dash.sh 30          # watch 30s
#   ./pr-dash.sh --once      # single render
#   ./pr-dash.sh --notify 30 # watch 30s with desktop notifications
#   ./pr-dash.sh [args...]   # any combination of pr_dashboard.py flags

export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -eq 0 ]]; then
    exec python3 "$DIR/pr_dashboard.py" --watch
elif [[ "$1" == "--once" ]]; then
    shift
    exec python3 "$DIR/pr_dashboard.py" "$@"
elif [[ "$1" =~ ^[0-9]+$ ]]; then
    interval="$1"; shift
    exec python3 "$DIR/pr_dashboard.py" --watch "$interval" "$@"
else
    exec python3 "$DIR/pr_dashboard.py" --watch "$@"
fi
