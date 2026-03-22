#!/bin/bash

# tmux_claude.sh - 管理 claude tmux 会话
#
# 用法:
#   tmux_claude.sh                           列出活动的 tmux sessions
#   tmux_claude.sh <dir> [start]             启动会话
#   tmux_claude.sh <dir> stop                停止会话
#   tmux_claude.sh <dir> restart             重启会话
#
# 选项:
#   --daemon      后台启动，不 attach tmux
#   --claude CMD  指定 claude 启动命令
#
# 配置文件: <dir>/tmux_claude.json
#   {
#     "auto_approve": false,
#     "load_md": false,
#     "detail": false,
#     "qq_bot": {"appid": "...", "secret": "...", "test_c2c_openid": "..."}
#   }

export LANG="C.UTF-8"
export LC_ALL="C.UTF-8"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LOG_SCRIPT="$SCRIPT_DIR/tmux_claude_log.py"

if ! command -v tmux &>/dev/null; then
    echo "错误: 未安装 tmux，请先安装: apt install tmux / yum install tmux"
    exit 1
fi

DEFAULT_CLAUDE_CMD="claude --effort max"

usage() {
    echo "用法: $0 <dir> [command] [options]"
    echo ""
    echo "命令:"
    echo "  start     启动会话 (默认)"
    echo "  stop      停止会话"
    echo "  restart   重启会话"
    echo ""
    echo "选项:"
    echo "  --daemon      后台启动，不 attach tmux"
    echo "  --all-yes     自动确认所有权限请求 (覆盖配置文件)"
    echo "  --claude CMD  指定 claude 启动命令 (默认: $DEFAULT_CLAUDE_CMD)"
    echo ""
    echo "配置文件: <dir>/tmux_claude.json"
    echo "  auto_approve: 自动确认权限"
    echo "  load_md: 启动时读取 CLAUDE.md"
    echo "  detail: 发送工具结果到 QQ"
    echo "  qq_bot: QQ Bot 配置 (有则启用)"
    echo ""
    echo "示例:"
    echo "  $0 /root/todo-list"
    echo "  $0 /root/todo-list --daemon"
    echo "  $0 /root/todo-list --all-yes"
    echo "  $0 /root/todo-list stop"
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
ALL_YES=false
CLAUDE_CMD="$DEFAULT_CLAUDE_CMD"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --daemon)
            DAEMON_MODE=true
            shift
            ;;
        --all-yes)
            ALL_YES=true
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

# 根据 claude 命令名推断数据目录
CLAUDE_NAME="${CLAUDE_BIN##*/}"
CLAUDE_DIR="$HOME/.$CLAUDE_NAME"

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

    # 构建 log 脚本参数
    LOG_ARGS="'$DIR_ABS' --session '$SESSION_NAME' --claude-dir '$CLAUDE_DIR'"
    if [[ "$ALL_YES" == "true" ]]; then
        LOG_ARGS="$LOG_ARGS --all-yes"
    fi

    # tmux 启动命令：log 守护进程后台运行，claude 前台
    TMUX_CMD="python3 '$LOG_SCRIPT' $LOG_ARGS >/dev/null & $CLAUDE_CMD --continue || $CLAUDE_CMD"

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
