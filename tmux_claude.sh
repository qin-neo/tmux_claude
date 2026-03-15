#!/bin/bash

# tmux_claude.sh - 管理 claude tmux 会话
#
# 用法:
#   tmux_claude.sh                    列出活动的 tmux sessions
#   tmux_claude.sh <目录>             在指定目录启动 claude tmux session
#   tmux_claude.sh <目录> all_yes     启动并自动确认所有权限请求
#   tmux_claude.sh <目录> --daemon    后台启动，不 attach tmux
#   tmux_claude.sh <目录> stop        停止指定目录的 tmux session 和 log 守护进程

export LANG="C.UTF-8"
export LC_ALL="C.UTF-8"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LOG_SCRIPT="$SCRIPT_DIR/tmux_claude_log.py"

if ! command -v tmux &>/dev/null; then
    echo "错误: 未安装 tmux，请先安装: apt install tmux / yum install tmux"
    exit 1
fi

# 检测 claude 命令
if command -v claude &>/dev/null; then
    CLAUDE_CMD="claude --effort max"
    CLAUDE_DIR="$HOME/.claude"
else
    echo "错误: 未安装 claude，请先安装: npm install -g @anthropic-ai/claude-code"
    exit 1
fi

usage() {
    echo "用法: $0 <目录> [stop|all_yes|--daemon]"
    echo "  <目录>    claude 的工作目录，同时作为 tmux session 名"
    echo "  all_yes   自动确认所有权限请求"
    echo "  --daemon  后台启动，不 attach tmux"
    echo "  stop      停止指定的 tmux session、log 守护进程和 QQ Bot"
    echo ""
    echo "Claude 命令: $CLAUDE_CMD"
    echo "QQ Bot: 若项目目录下存在 qq_bot_config.json 则自动启动"
    echo "加入 PATH: ln -s $SCRIPT_DIR/tmux_claude.sh /usr/local/bin/tmux_claude"
}

# 停止 log 守护进程
stop_log_daemon() {
    local session="$1"
    local pid
    pid=$(pgrep -f "tmux_claude_log.py --.*--session $session\\b" 2>/dev/null)
    if [[ -n "$pid" ]]; then
        kill $pid
        echo "已停止 log 守护进程 (PID: $pid)"
    fi
}

# 停止 QQ Bot
stop_qq_bot() {
    local session="$1"
    local pid
    pid=$(pgrep -f "qq_bot.py --.*--session $session\\b" 2>/dev/null)
    if [[ -n "$pid" ]]; then
        kill $pid
        echo "已停止 QQ Bot (PID: $pid)"
    fi
}

if [[ ! -f "$LOG_SCRIPT" ]]; then
    echo "错误: 找不到 $LOG_SCRIPT"
    exit 1
fi

if [[ $# -eq 0 ]]; then
    if tmux list-sessions 2>/dev/null; then
        exit 0
    fi
    echo "当前没有活动的 tmux sessions"
    echo ""
    usage
    exit 0
fi

DIR_ARG="${1%/}"
shift 2>/dev/null || true

# 解析参数
DAEMON_MODE=false
AUTO_APPROVE=false
ACTION=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        stop)
            ACTION="stop"
            shift
            ;;
        all_yes)
            AUTO_APPROVE=true
            shift
            ;;
        --daemon)
            DAEMON_MODE=true
            shift
            ;;
        *)
            echo "未知参数: $1"
            usage
            exit 1
            ;;
    esac
done

if [[ -z "$DIR_ARG" || "$DIR_ARG" == -* ]]; then
    usage
    exit 1
fi

DIR_ABS="$(cd "$DIR_ARG" 2>/dev/null && pwd)"
if [[ -z "$DIR_ABS" || ! -d "$DIR_ABS" ]]; then
    echo "错误: 目录 '$DIR_ARG' 不存在"
    exit 1
fi

# session 名取目录的 basename，替换 tmux 不允许的字符（. 和 :）
SESSION_NAME="$(basename "$DIR_ABS" | tr '.:' '_')"

if [[ "$ACTION" == "stop" ]]; then
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux kill-session -t "$SESSION_NAME"
        echo "已停止 tmux 会话 '$SESSION_NAME'"
    else
        echo "tmux 会话 '$SESSION_NAME' 不存在"
    fi
    stop_log_daemon "$SESSION_NAME"
    stop_qq_bot "$SESSION_NAME"
    exit 0
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "会话 '$SESSION_NAME' 已存在，正在进入..."
    exec tmux attach -d -t "$SESSION_NAME"
fi

tmux new-session -d -s "$SESSION_NAME" -c "$DIR_ABS" \
    "export LANG=C.UTF-8; $CLAUDE_CMD --continue || $CLAUDE_CMD"

tmux set -t "$SESSION_NAME" default-terminal "tmux-256color"
tmux set -ga -t "$SESSION_NAME" terminal-overrides ",xterm-256color:Tc"
tmux set -t "$SESSION_NAME" escape-time 10
tmux set -t "$SESSION_NAME" history-limit 50000
tmux set -t "$SESSION_NAME" mouse on
tmux unbind -n MouseDown3Pane

echo "已启动 tmux 会话 '$SESSION_NAME'"
echo "工作目录: $DIR_ABS"

# 检查是否需要启动 QQ Bot
QQ_CONFIG="$DIR_ABS/qq_bot_config.json"

if [[ -f "$QQ_CONFIG" ]]; then
    # 有 QQ 配置：启动 qq_bot.py（内嵌监听 + 写 log）
    QQ_SCRIPT="$SCRIPT_DIR/qq_bot.py"
    if [[ -f "$QQ_SCRIPT" ]]; then
        stop_qq_bot "$SESSION_NAME"
        QQ_ARGS=(
            --project-dir "$DIR_ABS"
            --session "$SESSION_NAME"
            --log-dir "$DIR_ABS"
            --claude-dir "$CLAUDE_DIR"
            --config "$QQ_CONFIG"
        )
        if [[ "$AUTO_APPROVE" == "true" ]]; then
            QQ_ARGS+=(--auto-approve)
            echo "已启用自动确认模式 (all_yes)"
        fi
        nohup python3 "$QQ_SCRIPT" "${QQ_ARGS[@]}" > /dev/null 2>&1 &
        echo "已启动 QQ Bot (PID: $!)"
        echo "日志文件: $DIR_ABS/tmux_claude.log"
    else
        echo "警告: 找不到 $QQ_SCRIPT，跳过 QQ Bot"
    fi
else
    # 无 QQ 配置：启动 log 守护进程
    stop_log_daemon "$SESSION_NAME"

    LOG_ARGS=(
        --project-dir "$DIR_ABS"
        --session "$SESSION_NAME"
        --log-dir "$DIR_ABS"
        --claude-dir "$CLAUDE_DIR"
    )
    if [[ "$AUTO_APPROVE" == "true" ]]; then
        LOG_ARGS+=(--auto-approve)
        echo "已启用自动确认模式 (all_yes)"
    fi

    nohup python3 "$LOG_SCRIPT" "${LOG_ARGS[@]}" > /dev/null 2>&1 &
    echo "已启动 log 守护进程 (PID: $!)"
    echo "日志文件: $DIR_ABS/tmux_claude.log"
fi

# daemon 模式不 attach，直接退出
if [[ "$DAEMON_MODE" == "true" ]]; then
    echo ""
    echo "后台模式启动完成"
    exit 0
fi

echo ""
exec tmux -u attach -d -t "$SESSION_NAME"
