#!/usr/bin/env python3
"""
对一个 claude project, 如果项目目录是 /opt/uas, 它会在 ~/.claude/projects 下面生成这个目录:
~/.claude/projects/-opt-uas/
其中会有一些 jsonl 的文件
-rw------- 1 root root 5757067 Mar 12 10:27 a209f32a-1de4-42e4-a93b-98a942827485.jsonl

使用 inotify 监听这个目录，文件有变化就解析 JSONL 记录到 tmux_claude.log 中。
"""

import sys
import time
import os
import json
import struct
import ctypes
import ctypes.util
import select
import subprocess
import signal
import logging
import argparse
import re
from logging.handlers import RotatingFileHandler

SESSION_CHECK_INTERVAL = 10.0

# inotify 常量
IN_MODIFY = 0x00000002
IN_CREATE = 0x00000100
IN_ISDIR = 0x40000000
_EVENT_HEADER_SIZE = 16  # wd(4) + mask(4) + cookie(4) + len(4)

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL)
_RE_BASH_STDOUT = re.compile(r"<bash-stdout>(.*?)</bash-stdout>", re.DOTALL)


class Inotify:
    """Linux inotify 的轻量 ctypes 封装"""
    def __init__(self):
        self.fd = _libc.inotify_init()
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init failed")
        self._wd_to_path = {}

    def add_watch(self, path, mask):
        wd = _libc.inotify_add_watch(self.fd, path.encode(), mask)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch failed: {path}")
        self._wd_to_path[wd] = path
        return wd

    def read_events(self, timeout):
        """读取事件，返回 (dir_path, mask, name) 列表。超时返回空列表。"""
        readable, _, _ = select.select([self.fd], [], [], timeout)
        if not readable:
            return []
        buf = os.read(self.fd, 8192)
        events = []
        offset = 0
        while offset < len(buf):
            wd, mask, _, name_len = struct.unpack_from("iIII", buf, offset)
            offset += _EVENT_HEADER_SIZE
            name = buf[offset:offset + name_len].rstrip(b"\x00").decode(errors="replace")
            offset += name_len
            events.append((self._wd_to_path.get(wd, ""), mask, name))
        return events

    def close(self):
        os.close(self.fd)


# ── 消息解析 ──

def _clean_text(text):
    """清理 ANSI 转义，保留换行结构"""
    text = _RE_ANSI.sub("", text)
    return text.strip()


def _truncate(s, maxlen=120):
    return s[:maxlen] + ("..." if len(s) > maxlen else "")


def _format_tool_use(name, inp):
    if name in ("Read", "ReadFile", "Write", "WriteFile", "Edit", "Replace"):
        return inp.get("file_path", inp.get("path", ""))
    if name in ("Grep", "Search"):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        return f"pattern={pattern}" + (f" path={path}" if path else "")
    if name == "Bash":
        return _truncate(inp.get("command", ""))
    if name in ("ListDir", "LS"):
        return inp.get("path", inp.get("dir_path", ""))
    if name == "AskUserQuestion":
        questions = inp.get("questions", [])
        parts = []
        for q in questions:
            question_text = q.get("question", "")
            options = q.get("options", [])
            if question_text:
                parts.append(question_text)
            for i, opt in enumerate(options, 1):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                parts.append(f"  {i}. {label}" + (f" - {desc}" if desc else ""))
        return "\n".join(parts) if parts else ""
    return _truncate(json.dumps(inp, ensure_ascii=False))


def extract_message(obj, state):
    """从 Claude CLI 格式 JSONL 对象中提取日志行。
    type=user/assistant/system，tool_use 嵌在 assistant content 中。
    返回 (lines, needs_approve)。
    """
    tp = obj.get("type")
    needs_approve = False

    if tp == "user":
        if obj.get("isMeta"):
            return [], False
        perm = obj.get("permissionMode")
        if perm and perm != state.get("permissionMode"):
            state["permissionMode"] = perm
        msg = obj.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str):
            m_input = _RE_BASH_INPUT.search(content)
            if m_input:
                return [f"[BASH] {m_input.group(1).strip()}"], False
            m_stdout = _RE_BASH_STDOUT.search(content)
            if m_stdout:
                text = _clean_text(m_stdout.group(1))
                return ([f"[BASH stdout]\n{text}"] if text else []), False
            text = _clean_text(content)
            return ([f"[USER] {text}"] if text else []), False
        if isinstance(content, list):
            lines = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tag = "TOOL ERROR" if block.get("is_error") else "TOOL RESULT"
                    result_content = block.get("content", "")
                    if isinstance(result_content, str):
                        text = _clean_text(result_content)
                    elif isinstance(result_content, list):
                        parts = []
                        for part in result_content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                parts.append(part.get("text", ""))
                        text = _clean_text("\n".join(parts))
                    else:
                        text = ""
                    if text:
                        lines.append(f"[{tag}]\n{text}")
                    else:
                        lines.append(f"[{tag}] (empty)")
                elif block.get("type") == "text":
                    text = _clean_text(block.get("text", ""))
                    if text:
                        lines.append(f"[USER] {text}")
            return lines, False
        return [], False

    if tp == "assistant":
        msg = obj.get("message", {})
        lines = []
        has_tool_use = False
        # bypassPermissions 时不需要审批，其他模式（default/acceptEdits/plan）都可能需要
        perm = state.get("permissionMode", "default")
        waiting = perm != "bypassPermissions"
        for block in msg.get("content", []):
            if block.get("type") == "text":
                text = _clean_text(block.get("text", ""))
                if text:
                    lines.append(f"[ASSISTANT] {text}")
            elif block.get("type") == "tool_use":
                has_tool_use = True
                name = block.get("name", "unknown")
                summary = _format_tool_use(name, block.get("input", {}))
                suffix = " (waiting for approval)" if waiting else ""
                lines.append(f"[TOOL USE] {name}: {summary}{suffix}")
        if has_tool_use and waiting:
            needs_approve = True
        return lines, needs_approve

    if tp == "system" and obj.get("subtype") == "turn_duration":
        return [f"[SYSTEM] turn duration: {obj.get('durationMs', 0)}ms"], False

    return [], False


def extract_message_generic(obj, state):
    """从 generic 格式 JSONL 对象中提取日志行。
    type=message(+role), function_call, function_call_result 等。
    返回 (lines, needs_approve)。
    """
    tp = obj.get("type")

    if tp == "message":
        role = obj.get("role")
        content = obj.get("content", [])
        if isinstance(content, str):
            text = _clean_text(content)
            if not text:
                return [], False
            tag = "USER" if role == "user" else "ASSISTANT"
            return [f"[{tag}] {text}"], False
        if isinstance(content, list):
            lines = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")
                text = _clean_text(block.get("text", ""))
                if not text:
                    continue
                if bt in ("input_text", "text") and role == "user":
                    lines.append(f"[USER] {text}")
                elif bt in ("output_text", "text") and role == "assistant":
                    lines.append(f"[ASSISTANT] {text}")
            return lines, False
        return [], False

    if tp == "function_call":
        name = obj.get("name", "unknown")
        args = obj.get("arguments", "")
        if isinstance(args, str):
            try:
                inp = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                inp = {}
        else:
            inp = args if isinstance(args, dict) else {}
        summary = _format_tool_use(name, inp)
        # 系统自动调度的工具不需要权限确认
        _NO_APPROVE = {"Agent", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TodoRead", "TodoWrite"}
        needs_approve = name not in _NO_APPROVE
        suffix = " (waiting for approval)" if needs_approve else ""
        return [f"[TOOL USE] {name}: {summary}{suffix}"], needs_approve

    if tp == "function_call_result":
        output = obj.get("output", "")
        if isinstance(output, dict):
            text = _clean_text(output.get("text", ""))
        elif isinstance(output, str):
            text = _clean_text(output)
        else:
            text = ""
        tag = "TOOL ERROR" if obj.get("status") == "error" else "TOOL RESULT"
        if text:
            return [f"[{tag}]\n{text}"], False
        return [f"[{tag}] (empty)"], False

    return [], False


def project_dir_to_internal(project_dir, claude_dir=None):
    """转换项目目录到内部目录名，兼容两种风格。
    - /opt/uas → -opt-uas (旧风格，前导 -, 下划线转连字符) → extract_message
    - /root/host_net_migrate → root-host_net_migrate (新风格，保留下划线) → extract_message_generic
    同时尝试 symlink resolve 后的路径。返回 (dir_name, extract_fn)。
    """
    real_dir = os.path.realpath(project_dir)
    candidates = [project_dir]
    if real_dir != project_dir:
        candidates.append(real_dir)

    if claude_dir:
        projects_dir = os.path.join(claude_dir, "projects")
        for d in candidates:
            base = d.lstrip("/").replace("/", "-")
            old_style = "-" + base.replace("_", "-")
            new_style = base
            if os.path.isdir(os.path.join(projects_dir, old_style)):
                return old_style, extract_message
            if os.path.isdir(os.path.join(projects_dir, new_style)):
                return new_style, extract_message_generic

    base = real_dir.lstrip("/").replace("/", "-")
    return base, extract_message_generic


# ── inotify 监控 ──

class JsonlTracker:
    """追踪单个 JSONL 文件的增量读取位置"""
    def __init__(self, path, skip_existing=True):
        self.path = path
        self.offset = os.path.getsize(path) if skip_existing and os.path.exists(path) else 0

    def read_new(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []

        if size < self.offset:
            self.offset = 0
        elif size == self.offset:
            return []

        try:
            with open(self.path, "r", errors="replace") as f:
                f.seek(self.offset)
                data = f.read()
                self.offset = f.tell()
        except OSError:
            return []

        objects = []
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                objects.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return objects


class ProjectWatcher:
    """用 inotify 监控 claude 项目目录下所有 JSONL 文件变化"""
    def __init__(self, internal_dir, skip_existing=True):
        self.internal_dir = internal_dir
        self.trackers = {}  # path → JsonlTracker
        self._inotify = Inotify()

        # watch 根目录（捕获新 jsonl 文件和新子目录）
        self._watch_dir(internal_dir)

        # 递归 watch 所有已有子目录
        for root, dirs, _files in os.walk(internal_dir):
            for d in dirs:
                self._watch_dir(os.path.join(root, d))

        # 初始化已有文件的 tracker
        for entry in self._iter_jsonl_files():
            self.trackers[entry] = JsonlTracker(entry, skip_existing=skip_existing)
            logging.getLogger("claude_log").debug("track file: %s (offset=%d)", entry, self.trackers[entry].offset)

    def _watch_dir(self, path):
        self._inotify.add_watch(path, IN_MODIFY | IN_CREATE)
        logging.getLogger("claude_log").debug("watch dir: %s", path)

    def _iter_jsonl_files(self):
        for root, dirs, files in os.walk(self.internal_dir):
            for f in files:
                if f.endswith(".jsonl"):
                    yield os.path.join(root, f)

    def poll(self, timeout):
        """等待 inotify 事件，返回新的 JSONL 对象列表"""
        events = self._inotify.read_events(timeout)
        log = logging.getLogger("claude_log")
        modified_files = set()

        for dir_path, mask, name in events:
            full_path = os.path.join(dir_path, name)
            log.debug("inotify event: mask=0x%x name=%s dir=%s", mask, name, dir_path)

            if mask & IN_CREATE and mask & IN_ISDIR:
                self._watch_dir(full_path)
                continue

            if not name.endswith(".jsonl"):
                continue

            if mask & IN_CREATE and full_path not in self.trackers:
                self.trackers[full_path] = JsonlTracker(full_path, skip_existing=False)
                log.debug("new file tracker: %s", full_path)

            modified_files.add(full_path)

        results = []
        for path in modified_files:
            tracker = self.trackers.get(path)
            if tracker:
                objs = tracker.read_new()
                if objs:
                    log.debug("read %d objects from %s", len(objs), path)
                results.extend(objs)
        return results

    def close(self):
        self._inotify.close()


# ── 工具函数 ──

def setup_logging(log_file):
    """RotatingFileHandler，单文件 10MB，最多 100 个备份"""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logger = logging.getLogger("claude_log")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=100, encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def check_tmux_session(session_name):
    """检查 tmux session 是否存在"""
    return subprocess.run(
        ["tmux", "has-session", "-t", session_name], capture_output=True,
    ).returncode == 0


def send_to_tmux(session_name, text):
    """发送文本到 tmux session"""
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, text, "Enter"],
        capture_output=True,
    )


def send_approve(session_name):
    """通过 tmux 发送 Enter 自动确认权限请求"""
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, "Enter"],
        capture_output=True,
    )


CLAUDEMD_INTERVAL = 24 * 3600  # 24小时
CLAUDEMD_PROMPT = "读 CLAUDE.md,遵守指示"


def send_claudemd_prompt(session_name):
    """发送 CLAUDE.md 读取指示"""
    send_to_tmux(session_name, CLAUDEMD_PROMPT)


def check_claudemd_refresh(session_name, last_check, interval=CLAUDEMD_INTERVAL):
    """检查是否需要刷新 CLAUDE.md，返回新的 last_check"""
    now = time.monotonic()
    if now - last_check >= interval:
        send_claudemd_prompt(session_name)
        return now
    return last_check


# ── 主循环 ──

def watch_loop(watcher, logger, session_name, stop_event, auto_approve,
               extract_fn=extract_message, load_md=False, on_line=None):
    """主监听循环。
    on_line(line): 可选回调，每条日志行调用一次（供 QQ bot 等外部消费）。
    """
    last_session_check = time.monotonic()
    last_claudemd_read = time.monotonic()
    state = {}
    pending_approve = None  # (发送时间, 已重试次数)

    while not stop_event["stop"]:
        poll_timeout = 1.0 if pending_approve else SESSION_CHECK_INTERVAL
        got_result = False

        for obj in watcher.poll(timeout=poll_timeout):
            lines, needs_approve = extract_fn(obj, state)
            for line in lines:
                logger.info(line)
                if on_line:
                    on_line(line)
                if line.startswith("[TOOL RESULT]") or line.startswith("[TOOL ERROR]"):
                    got_result = True
            if needs_approve and auto_approve:
                send_approve(session_name)
                logger.info("[tmux_claude auto approve]")
                pending_approve = (time.monotonic(), 0)

        # 已收到 tool result，清除等待状态
        if got_result:
            pending_approve = None

        # 超过 1 秒未收到 tool result，重发一次 Enter
        if pending_approve:
            sent_time, retries = pending_approve
            if retries < 1 and time.monotonic() - sent_time >= 1.0:
                send_approve(session_name)
                logger.info("[tmux_claude auto approve] retry")
                pending_approve = (sent_time, retries + 1)

        now = time.monotonic()
        if now - last_session_check >= SESSION_CHECK_INTERVAL:
            last_session_check = now
            if not check_tmux_session(session_name):
                print(f"[INFO] tmux session '{session_name}' 已结束，退出", file=sys.stderr)
                break

        if load_md:
            last_claudemd_read = check_claudemd_refresh(session_name, last_claudemd_read)


def load_config(project_dir):
    """加载配置文件，不存在则创建默认配置"""
    config_path = os.path.join(project_dir, "tmux_claude.json")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)

    # 创建默认配置文件
    default_config = {
        "auto_approve": False,
        "load_md": False,
        "detail": False,
    }
    with open(config_path, "w") as f:
        json.dump(default_config, f, indent=2)
    print(f"[INFO] 已创建默认配置文件: {config_path}", file=sys.stderr)
    return default_config


def main():
    parser = argparse.ArgumentParser(description="claude JSONL 文件监控 log 守护进程")
    parser.add_argument("project_dir", help="claude 项目的绝对路径")
    parser.add_argument("--session", help="tmux session 名称 (默认: 目录名)")
    parser.add_argument("--claude-dir", default="~/.claude", help="claude 数据目录")
    parser.add_argument("--all-yes", action="store_true", help="自动确认所有权限请求 (覆盖配置文件)")
    args = parser.parse_args()

    project_dir = os.path.abspath(os.path.expanduser(args.project_dir))
    claude_dir = os.path.abspath(os.path.expanduser(args.claude_dir))
    session = args.session or os.path.basename(project_dir).replace(".", "_").replace(":", "_")

    if not os.path.isdir(project_dir):
        print(f"错误: 目录不存在: {project_dir}", file=sys.stderr)
        sys.exit(1)

    # 加载配置
    config = load_config(project_dir)
    # --all-yes 时更新配置文件
    if args.all_yes and not config.get("auto_approve", False):
        config["auto_approve"] = True
        config_path = os.path.join(project_dir, "tmux_claude.json")
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
    auto_approve = config.get("auto_approve", False)
    load_md = config.get("load_md", False)
    detail = config.get("detail", False)
    qq_config = config.get("qq_bot")

    internal_name, extract_fn = project_dir_to_internal(project_dir, claude_dir)
    internal_dir = os.path.join(claude_dir, "projects", internal_name)

    # 有 QQ Bot 配置则启动 QQ Bot
    if qq_config:
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if script_dir not in sys.path:
                sys.path.insert(0, script_dir)
            from qq_bot import run_qq_bot
            run_qq_bot(
                session=session,
                project_dir=project_dir,
                log_dir=project_dir,
                internal_dir=internal_dir,
                qq_config=qq_config,
                auto_approve=auto_approve,
                load_md=load_md,
                detail=detail,
                extract_fn=extract_fn,
            )
        except ImportError as e:
            print(f"错误: 无法加载 QQ Bot 模块: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # 无 QQ 配置：运行普通 log 守护进程
    log_file = os.path.join(project_dir, "tmux_claude.log")
    logger = setup_logging(log_file)

    mode_str = " (auto-approve)" if auto_approve else ""
    fn_name = extract_fn.__name__
    print(f"[INFO] claude_log 启动{mode_str} [{fn_name}]: project={project_dir}, log={log_file}", file=sys.stderr)

    # 等待 claude 数据目录就绪
    if not os.path.isdir(internal_dir):
        print(f"[INFO] 等待 claude 数据目录: {internal_dir}", file=sys.stderr)
        while not os.path.isdir(internal_dir):
            time.sleep(1)
        print(f"[INFO] claude 数据目录已就绪", file=sys.stderr)

    watcher = ProjectWatcher(internal_dir, skip_existing=True)

    stop_event = {"stop": False}

    def signal_handler(signum, frame):
        print(f"\n[INFO] 收到信号 {signum}，正在退出...", file=sys.stderr)
        stop_event["stop"] = True

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        watch_loop(watcher, logger, session, stop_event, auto_approve, extract_fn, load_md)
    finally:
        watcher.close()
        print(f"[INFO] claude_log 已退出: project={project_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
