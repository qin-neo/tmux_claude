#!/bin/bash
# restart.sh - 重启 tmux_claude
# 用法: ./restart.sh [all_yes]
# 如果不传参数，自动检测当前模式并保持

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
SESSION_NAME="$(basename "$PROJECT_DIR" | tr '.:' '_')"

# 检测当前是否有 --auto-approve
detect_auto_approve() {
    local session="$1"
    # 检查 qq_bot 或 log_daemon 是否有 --auto-approve 参数
    if pgrep -f "qq_bot.py.*--session ${session}.*--auto-approve" > /dev/null 2>&1; then
        return 0
    fi
    if pgrep -f "tmux_claude_log.py.*--session ${session}.*--auto-approve" > /dev/null 2>&1; then
        return 0
    fi
    return 1
}

# 停止（发送 /exit，等待退出）
"$SCRIPT_DIR/tmux_claude.sh" "$PROJECT_DIR" stop

# 确定启动参数
RESTART_ARGS=""
if [[ -n "$1" ]]; then
    # 有参数则使用参数
    RESTART_ARGS="$1"
elif detect_auto_approve "$SESSION_NAME"; then
    # 无参数但检测到之前是 all_yes 模式
    RESTART_ARGS="all_yes"
fi

# 启动
"$SCRIPT_DIR/tmux_claude.sh" "$PROJECT_DIR" $RESTART_ARGS --daemon
