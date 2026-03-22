#!/bin/bash

# tmux_claude.sh - 管理 claude tmux 会话
#
# 用法:
#   tmux_claude.sh                           列出活动的 tmux sessions
#   tmux_claude.sh <dir> start [options]     启动会话
#   tmux_claude.sh <dir> stop                停止会话
#   tmux_claude.sh <dir> restart [options]   重启会话
#
# 选项:
#   --all-yes     自动确认所有权限请求
#   --daemon      后台启动，不 attach tmux
#   --claude CMD  指定 claude 启动命令

export LANG="C.UTF-8"
export LC_ALL="C.UTF-8"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LOG_SCRIPT="$SCRIPT_DIR/tmux_claude_log.py"

if ! command -v tmux &>/dev/null; then
    echo "错误: 未安装 tmux，请先安装: apt install tmux / yum install tmux"
    exit 1
fi

CLAUDE_DIR="$HOME/.claude"
DEFAULT_CLAUDE_CMD="claude --effort max"

usage() {
    echo "用法: $0 <dir> <command> [options]"
    echo ""
    echo "命令:"
    echo "  start     启动会话"
    echo "  stop      停止会话"
    echo "  restart   重启会话"
    echo ""
    echo "选项:"
    echo "  --all-yes     自动确认所有权限请求"
    echo "  --daemon      后台启动，不 attach tmux"
    echo "  --load-md     启动时读取 CLAUDE.md"
    echo "  --detail      发送工具结果到 QQ (仅 QQ Bot，默认只发工具调用)"
    echo "  --claude CMD  指定 claude 启动命令 (默认: $DEFAULT_CLAUDE_CMD)"
    echo ""
    echo "示例:"
    echo "  $0 /root/todo-list start --all-yes --daemon"
    echo "  $0 /root/todo-list stop"
    echo "  $0 /root/todo-list restart"
    echo ""
    echo "QQ Bot: 若项目目录下存在 qq_bot_config.json 则自动启动"
}

if [[ ! -f "$LOG_SCRIPT" ]]; then
    echo "错误: 找不到 $LOG_SCRIPT"
    exit 1
fi

# 无参数：列出 sessions
if [[ $# -eq 0 ]]; then
    if tmux list-sessions 2>/dev/null; then
        exit 0
    fi
    echo "当前没有活动的 tmux sessions"
    echo ""
    usage
    exit 0
fi

# 解析目录和命令
DIR_ARG="${1%/}"
shift 2>/dev/null || true

# 如果第一个参数是选项或为空，报错
if [[ -z "$DIR_ARG" || "$DIR_ARG" == -* ]]; then
    usage
    exit 1
fi

# 获取命令
COMMAND="${1:-start}"
case "$COMMAND" in
    start|stop|restart)
        shift
        ;;
    -*)
        # 选项，默认为 start
        COMMAND="start"
        ;;
    *)
        echo "错误: 未知命令 '$COMMAND'"
        usage
        exit 1
        ;;
esac

# 解析选项
DAEMON_MODE=false
AUTO_APPROVE=false
LOAD_MD=false
DETAIL=false
CLAUDE_CMD="$DEFAULT_CLAUDE_CMD"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all-yes)
            AUTO_APPROVE=true
            shift
            ;;
        --daemon)
            DAEMON_MODE=true
            shift
            ;;
        --load-md)
            LOAD_MD=true
            shift
            ;;
        --detail)
            DETAIL=true
            shift
            ;;
        --claude)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "错误: --claude 需要参数"
                exit 1
            fi
            CLAUDE_CMD="$2"
            shift 2
            ;;
        *)
            echo "错误: 未知选项 '$1'"
            usage
            exit 1
            ;;
    esac
done

# 检测 claude 二进制是否存在
CLAUDE_BIN="${CLAUDE_CMD%% *}"
if ! command -v "$CLAUDE_BIN" &>/dev/null; then
    echo "错误: 未找到命令 '$CLAUDE_BIN'"
    echo "请安装 Claude CLI 或使用 --claude 指定命令"
    exit 1
fi

DIR_ABS="$(cd "$DIR_ARG" 2>/dev/null && pwd)"
if [[ -z "$DIR_ABS" || ! -d "$DIR_ABS" ]]; then
    echo "错误: 目录 '$DIR_ARG' 不存在"
    exit 1
fi

# session 名取目录的 basename，替换 tmux 不允许的字符（. 和 :）
SESSION_NAME="$(basename "$DIR_ABS" | tr '.:' '_')"

# === stop 命令 ===
do_stop() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux send-keys -t "$SESSION_NAME" "/exit" Enter
        echo "已发送 /exit 到 tmux 会话 '$SESSION_NAME'"
        for i in {1..30}; do
            if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
            echo "tmux 会话 '$SESSION_NAME' 未响应 /exit"
        else
            echo "tmux 会话 '$SESSION_NAME' 已退出"
        fi
    else
        echo "tmux 会话 '$SESSION_NAME' 不存在"
    fi
}

# === start 命令 ===
do_start() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "会话 '$SESSION_NAME' 已存在，正在进入..."
        exec tmux attach -d -t "$SESSION_NAME"
    fi

    # 构建 tmux 启动命令
    QQ_CONFIG="$DIR_ABS/qq_bot_config.json"
    TMUX_CMD="export LANG=C.UTF-8; $CLAUDE_CMD --continue || $CLAUDE_CMD"

    if [[ -f "$QQ_CONFIG" ]]; then
        # 有 QQ 配置：qq_bot 后台运行，claude 前台
        QQ_SCRIPT="$SCRIPT_DIR/qq_bot.py"
        if [[ -f "$QQ_SCRIPT" ]]; then
            QQ_ARGS="--project-dir '$DIR_ABS' --session '$SESSION_NAME' --log-dir '$DIR_ABS' --claude-dir '$CLAUDE_DIR' --config '$QQ_CONFIG'"
            if [[ "$AUTO_APPROVE" == "true" ]]; then
                QQ_ARGS="$QQ_ARGS --auto-approve"
                echo "已启用自动确认模式 (--all-yes)"
            fi
            if [[ "$LOAD_MD" == "true" ]]; then
                QQ_ARGS="$QQ_ARGS --load-md"
            fi
            if [[ "$DETAIL" == "true" ]]; then
                QQ_ARGS="$QQ_ARGS --detail"
            fi
            TMUX_CMD="python3 '$QQ_SCRIPT' $QQ_ARGS > /dev/null 2>&1 & $TMUX_CMD"
        else
            echo "警告: 找不到 $QQ_SCRIPT，跳过 QQ Bot"
        fi
    else
        # 无 QQ 配置：启动 log 守护进程（作为 tmux 子进程）
        LOG_ARGS="--project-dir '$DIR_ABS' --session '$SESSION_NAME' --log-dir '$DIR_ABS' --claude-dir '$CLAUDE_DIR'"
        if [[ "$AUTO_APPROVE" == "true" ]]; then
            LOG_ARGS="$LOG_ARGS --auto-approve"
            echo "已启用自动确认模式 (--all-yes)"
        fi
        if [[ "$LOAD_MD" == "true" ]]; then
            LOG_ARGS="$LOG_ARGS --load-md"
        fi
        TMUX_CMD="python3 '$LOG_SCRIPT' $LOG_ARGS > /dev/null 2>&1 & $TMUX_CMD"
    fi

    # 启动 tmux session
    tmux new-session -d -s "$SESSION_NAME" -c "$DIR_ABS" "$TMUX_CMD"

    tmux set -t "$SESSION_NAME" default-terminal "tmux-256color"
    tmux set -ga -t "$SESSION_NAME" terminal-overrides ",xterm-256color:Tc"
    tmux set -t "$SESSION_NAME" escape-time 10
    tmux set -t "$SESSION_NAME" history-limit 50000
    tmux set -t "$SESSION_NAME" mouse on
    tmux unbind -n MouseDown3Pane

    echo "已启动 tmux 会话 '$SESSION_NAME'"
    echo "工作目录: $DIR_ABS"
    echo "日志文件: $DIR_ABS/tmux_claude.log"

    # daemon 模式不 attach，直接退出
    if [[ "$DAEMON_MODE" == "true" ]]; then
        echo ""
        echo "后台模式启动完成"
        exit 0
    fi

    echo ""
    exec tmux -u attach -d -t "$SESSION_NAME"
}

# === restart 命令 ===
do_restart() {
    # 先检测当前是否有 --auto-approve（在 stop 之前）
    if pgrep -f "qq_bot.py.*--session ${SESSION_NAME}.*--auto-approve" > /dev/null 2>&1; then
        AUTO_APPROVE=true
    elif pgrep -f "tmux_claude_log.py.*--session ${SESSION_NAME}.*--auto-approve" > /dev/null 2>&1; then
        AUTO_APPROVE=true
    fi

    do_stop
    sleep 1
    do_start
}

# 执行命令
case "$COMMAND" in
    stop) do_stop ;;
    start) do_start ;;
    restart) do_restart ;;
esac
