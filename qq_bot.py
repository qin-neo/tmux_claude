#!/usr/bin/env python3
"""
QQ Bot for tmux_claude - QQ 远程控制 Claude CLI

通过 QQ 消息与 Claude 交互：
- 接收 QQ 消息 → 发送到 tmux session
- 监听 tmux_claude.log → 发送 Claude 回复到 QQ
"""

import sys
import os
import json
import time
import asyncio
import argparse
import subprocess
import signal
import logging
import re
from logging.handlers import RotatingFileHandler

try:
    import botpy
    from botpy import logging as botpy_logging
    from botpy.message import Message, DirectMessage
except ImportError:
    print("错误: 未安装 qq-botpy，请运行: pip install qq-botpy", file=sys.stderr)
    sys.exit(1)

# 回复完成标记
TURN_END_MARKER = re.compile(r"\[SYSTEM\] turn duration: \d+ms")

# 最大回复长度（QQ 消息限制）
MAX_MESSAGE_LENGTH = 2000


def project_dir_to_internal(project_dir):
    """/root/chcgw_probe → -root-chcgw-probe"""
    return project_dir.replace("/", "-").replace("_", "-")


def send_to_tmux(session, text):
    """发送文本到 tmux session"""
    # 转义特殊字符
    escaped = text.replace("'", "'\\''")
    subprocess.run(
        ["tmux", "send-keys", "-t", session, escaped, "Enter"],
        capture_output=True,
    )


class LogWatcher:
    """监听 tmux_claude.log 文件变化"""

    def __init__(self, log_file, skip_existing=True):
        self.log_file = log_file
        self.offset = os.path.getsize(log_file) if skip_existing and os.path.exists(log_file) else 0
        self._last_check = 0
        self._last_size = 0

    def read_new(self):
        """读取新增内容，返回行列表"""
        if not os.path.exists(self.log_file):
            return []

        try:
            size = os.path.getsize(self.log_file)
        except OSError:
            return []

        if size < self.offset:
            self.offset = 0
        elif size == self.offset:
            return []

        try:
            with open(self.log_file, "r", errors="replace") as f:
                f.seek(self.offset)
                data = f.read()
                self.offset = f.tell()
        except OSError:
            return []

        lines = []
        for line in data.splitlines():
            line = line.strip()
            if line:
                lines.append(line)
        return lines


class ClaudeBot(botpy.Client):
    """QQ Bot 客户端"""

    def __init__(self, session, log_file, logger, **kwargs):
        # 移除 logger 参数，botpy.Client 不接受
        super().__init__(**kwargs)
        self.session = session
        self.log_watcher = LogWatcher(log_file, skip_existing=True)
        self._pending_replies = {}  # channel_id/group_id -> {"accumulated": [], "last_time": float}
        self._reply_cooldown = 2.0  # 秒，等待更多输出
        self._check_interval = 0.5  # 秒，轮询间隔
        self._external_logger = logger  # 使用外部 logger
        self._test_channel_id = None  # 测试消息目标频道
        self._test_group_id = None    # 测试消息目标群
        self._test_c2c_openid = None  # 测试消息目标用户 (C2C单聊)
        self._config_path = None      # 配置文件路径

    def set_test_target(self, channel_id=None, group_id=None, c2c_openid=None, config_path=None):
        """设置测试消息目标"""
        self._test_channel_id = channel_id
        self._test_group_id = group_id
        self._test_c2c_openid = c2c_openid
        self._config_path = config_path

    async def on_ready(self):
        """Bot 就绪"""
        self._external_logger.info(f"Claude Bot 已就绪，session: {self.session}")
        # 发送上线通知
        await self._send_online_notification()

    async def _send_online_notification(self):
        """发送上线通知到指定频道/群/用户"""
        import asyncio
        # 等待 1 秒确保连接稳定
        await asyncio.sleep(1)

        msg = f"🤖 Claude Bot 已上线！session: {self.session}"

        # 发送到频道
        if self._test_channel_id:
            try:
                await self.api.post_message(
                    channel_id=self._test_channel_id,
                    content=msg
                )
                self._external_logger.info(f"已发送上线通知到频道: {self._test_channel_id}")
            except Exception as e:
                self._external_logger.error(f"发送频道消息失败: {e}")

        # 发送到群
        if self._test_group_id:
            try:
                await self.api.post_group_message(
                    group_openid=self._test_group_id,
                    content=msg
                )
                self._external_logger.info(f"已发送上线通知到群: {self._test_group_id}")
            except Exception as e:
                self._external_logger.error(f"发送群消息失败: {e}")

        # 发送到 C2C 单聊用户
        if self._test_c2c_openid:
            try:
                await self.api.post_c2c_message(
                    openid=self._test_c2c_openid,
                    content=msg
                )
                self._external_logger.info(f"已发送上线通知到用户: {self._test_c2c_openid}")
            except Exception as e:
                self._external_logger.error(f"发送C2C消息失败: {e}")

    async def on_at_message_create(self, message: Message):
        """处理频道 @ 消息"""
        self._external_logger.info(f"收到频道消息: {message.content}")
        await self._handle_message(message, message.channel_id, "channel")

    async def on_group_at_message_create(self, message: Message):
        """处理群 @ 消息"""
        self._external_logger.info(f"收到群消息: {message.content}")
        await self._handle_message(message, message.group_id, "group")

    async def on_direct_message_create(self, message: DirectMessage):
        """处理私聊消息"""
        self._external_logger.info(f"收到私聊消息: {message.content}")
        await self._handle_message(message, message.guild_id, "direct")

    async def on_c2c_message_create(self, message):
        """处理 C2C 单聊消息"""
        self._external_logger.info(f"收到C2C消息: {message}")

        # 提取并保存 openid
        openid = None
        if hasattr(message, 'author') and message.author:
            author = message.author
            if isinstance(author, dict):
                openid = author.get('user_openid')
            elif hasattr(author, 'user_openid'):
                openid = author.user_openid

        if openid and openid != self._test_c2c_openid:
            self._test_c2c_openid = openid
            self._external_logger.info(f"记录用户 openid: {openid}")
            # 保存到配置文件
            self._save_openid_to_config(openid)

        await self._handle_c2c_message(message)

    def _save_openid_to_config(self, openid):
        """保存 openid 到配置文件"""
        try:
            config_path = self._config_path
            if config_path and os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                config['test_c2c_openid'] = openid
                with open(config_path, 'w') as f:
                    json.dump(config, f, indent=4)
                self._external_logger.info(f"已保存 openid 到配置文件: {config_path}")
        except Exception as e:
            self._external_logger.error(f"保存 openid 失败: {e}")

    async def _handle_message(self, message, target_id, msg_type):
        """处理消息：发送到 tmux，等待回复"""
        # 提取消息内容（去除 @ 部分）
        content = message.content
        if hasattr(message, "mentions") and message.mentions:
            # 移除 @ 用户的部分
            for mention in message.mentions:
                content = content.replace(f"<@!{mention.id}>", "").strip()

        content = content.strip()
        if not content:
            return

        # 发送到 tmux
        self._external_logger.info(f"发送到 tmux [{self.session}]: {content}")
        send_to_tmux(self.session, content)

        # 等待并收集回复
        await self._wait_and_reply(message, target_id, msg_type)

    async def _handle_c2c_message(self, message):
        """处理 C2C 单聊消息"""
        content = message.content.strip()
        if not content:
            return

        # 发送到 tmux
        self._external_logger.info(f"发送到 tmux [{self.session}]: {content}")
        send_to_tmux(self.session, content)

        # 等待并收集回复
        await self._wait_and_reply_c2c(message)

    async def _wait_and_reply(self, message, target_id, msg_type):
        """等待 Claude 回复并发送"""
        start_time = time.monotonic()
        timeout = 120.0  # 最长等待 2 分钟
        accumulated = []
        last_activity = time.monotonic()

        while time.monotonic() - start_time < timeout:
            lines = self.log_watcher.read_new()
            turn_ended = False

            for line in lines:
                # 解析日志行
                # 格式: 2025-03-15 10:00:00,000 INFO [ASSISTANT] ...
                parts = line.split(" INFO ", 1)
                if len(parts) < 2:
                    continue

                log_content = parts[1]

                # 收集 ASSISTANT 输出
                if log_content.startswith("[ASSISTANT]"):
                    text = log_content[len("[ASSISTANT]"):].strip()
                    if text:
                        accumulated.append(text)
                    last_activity = time.monotonic()

                # 检测对话结束
                if TURN_END_MARKER.search(log_content):
                    turn_ended = True
                    last_activity = time.monotonic()

            # 如果对话结束或超过冷却时间无新输出，发送回复
            if turn_ended or (accumulated and time.monotonic() - last_activity > self._reply_cooldown):
                break

            await asyncio.sleep(self._check_interval)

        # 发送收集到的回复
        if accumulated:
            reply_text = "\n".join(accumulated)
            await self._send_reply(message, target_id, msg_type, reply_text)

    async def _send_reply(self, message, target_id, msg_type, text):
        """发送回复到 QQ"""
        # 分割长消息
        chunks = self._split_message(text)

        for chunk in chunks:
            try:
                if msg_type == "channel":
                    await message.reply(content=chunk)
                elif msg_type == "group":
                    await message.reply(content=chunk)
                else:  # direct
                    await message.reply(content=chunk)
                self._external_logger.info(f"已发送回复 ({len(chunk)} 字符)")
            except Exception as e:
                self._external_logger.error(f"发送回复失败: {e}")

    async def _wait_and_reply_c2c(self, message):
        """等待 Claude 回复并发送 C2C 消息"""
        start_time = time.monotonic()
        timeout = 120.0  # 最长等待 2 分钟
        accumulated = []
        last_activity = time.monotonic()

        while time.monotonic() - start_time < timeout:
            lines = self.log_watcher.read_new()
            turn_ended = False

            for line in lines:
                parts = line.split(" INFO ", 1)
                if len(parts) < 2:
                    continue

                log_content = parts[1]

                if log_content.startswith("[ASSISTANT]"):
                    text = log_content[len("[ASSISTANT]"):].strip()
                    if text:
                        accumulated.append(text)
                    last_activity = time.monotonic()

                if TURN_END_MARKER.search(log_content):
                    turn_ended = True
                    last_activity = time.monotonic()

            if turn_ended or (accumulated and time.monotonic() - last_activity > self._reply_cooldown):
                break

            await asyncio.sleep(self._check_interval)

        # 发送收集到的回复
        if accumulated:
            reply_text = "\n".join(accumulated)
            chunks = self._split_message(reply_text)
            for chunk in chunks:
                try:
                    await message.reply(content=chunk)
                    self._external_logger.info(f"已发送C2C回复 ({len(chunk)} 字符)")
                except Exception as e:
                    self._external_logger.error(f"发送C2C回复失败: {e}")

    def _split_message(self, text):
        """分割长消息"""
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]

        chunks = []
        lines = text.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 > MAX_MESSAGE_LENGTH:
                if current:
                    chunks.append(current.strip())
                current = line
            else:
                current = current + "\n" + line if current else line

        if current:
            chunks.append(current.strip())

        return chunks


def setup_logging(log_file=None):
    """设置日志"""
    logger = logging.getLogger("qq_bot")
    logger.setLevel(logging.INFO)

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(console_handler)

    # 文件输出
    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(file_handler)

    return logger


def check_tmux_session(session_name):
    """检查 tmux session 是否存在"""
    return subprocess.run(
        ["tmux", "has-session", "-t", session_name], capture_output=True
    ).returncode == 0


def main():
    parser = argparse.ArgumentParser(description="QQ Bot for tmux_claude")
    parser.add_argument("--project-dir", required=True, help="claude 项目的绝对路径")
    parser.add_argument("--session", required=True, help="tmux session 名称")
    parser.add_argument("--log-dir", required=True, help="log 文件存放目录")
    parser.add_argument("--claude-dir", required=True, help="claude 数据目录 (~/.claude)")
    parser.add_argument("--config", required=True, help="QQ Bot 配置文件路径")
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)
    log_dir = os.path.abspath(args.log_dir)

    # 检查配置文件
    if not os.path.exists(args.config):
        print(f"错误: 配置文件不存在: {args.config}", file=sys.stderr)
        sys.exit(1)

    with open(args.config, "r") as f:
        config = json.load(f)

    appid = config.get("appid")
    secret = config.get("secret")
    test_channel_id = config.get("test_channel_id")  # 测试消息目标频道
    test_group_id = config.get("test_group_id")      # 测试消息目标群
    test_c2c_openid = config.get("test_c2c_openid")  # 测试消息目标用户 (C2C单聊)

    if not appid or not secret:
        print("错误: 配置文件缺少 appid 或 secret", file=sys.stderr)
        sys.exit(1)

    # 检查 tmux session
    if not check_tmux_session(args.session):
        print(f"错误: tmux session '{args.session}' 不存在", file=sys.stderr)
        sys.exit(1)

    # 日志文件
    log_file = os.path.join(log_dir, "qq_bot.log")
    logger = setup_logging(log_file)

    # tmux_claude.log 路径
    claude_log = os.path.join(log_dir, "tmux_claude.log")

    print(f"[INFO] QQ Bot 启动: session={args.session}, log={log_file}", file=sys.stderr)

    # 创建 Bot
    intents = botpy.Intents(
        public_guild_messages=True,  # 频道 @ 消息
        public_messages=True,         # 群 @ 消息
        direct_message=True,          # 私信
    )

    client = ClaudeBot(
        session=args.session,
        log_file=claude_log,
        logger=logger,
        intents=intents,
    )

    # 设置测试消息目标和配置文件路径
    client.set_test_target(
        channel_id=test_channel_id,
        group_id=test_group_id,
        c2c_openid=test_c2c_openid,
        config_path=args.config,
    )

    # 运行 Bot
    client.run(appid=appid, secret=secret)


if __name__ == "__main__":
    main()
