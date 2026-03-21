#!/bin/bash
# restart.sh - 重启 tmux_claude
# 用法: ./restart.sh [all_yes]

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

# 停止（发送 /exit，等待退出）
"$SCRIPT_DIR/tmux_claude.sh" "$PROJECT_DIR" stop

# 启动
"$SCRIPT_DIR/tmux_claude.sh" "$PROJECT_DIR" ${1:-} --daemon
