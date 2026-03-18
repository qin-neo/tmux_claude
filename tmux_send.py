#!/usr/bin/env python3
"""向 tmux session 发送消息，用于 crontab 调用"""

import subprocess
import sys


def check_session(session: str) -> bool:
    """检查 tmux session 是否存在"""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def send_to_tmux(session: str, text: str) -> bool:
    """发送文本到 tmux session，返回是否成功"""
    escaped = text.replace("'", "'\\''")
    result = subprocess.run(
        ["tmux", "send-keys", "-t", session, escaped, "Enter"],
        capture_output=True,
    )
    return result.returncode == 0


def main():
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <session> <message>", file=sys.stderr)
        sys.exit(1)

    session = sys.argv[1]
    message = " ".join(sys.argv[2:])

    if not check_session(session):
        print(f"错误: tmux session '{session}' 不存在", file=sys.stderr)
        sys.exit(1)

    if send_to_tmux(session, message):
        print(f"已发送到 '{session}': {message}")
    else:
        print(f"发送失败", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
