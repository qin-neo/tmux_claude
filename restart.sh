#!/bin/bash
# restart.sh - 重启 tmux_claude
# 用法: ./restart.sh [--all-yes]
# 如果不传参数，自动检测当前模式并保持

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

# restart 已内置自动检测，直接调用
exec "$SCRIPT_DIR/tmux_claude.sh" "$PROJECT_DIR" restart --daemon "$@"
