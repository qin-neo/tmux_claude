#!/usr/bin/env python3
"""
QQ Bot for tmux_claude - QQ 远程控制 Claude CLI

通过 QQ 消息与 Claude 交互：
- 接收 QQ 消息 → 发送到 tmux session
- 监听 JSONL 文件 → 写 log + 发送 Claude 回复到 QQ

复用 tmux_claude_log.py 的监听逻辑，无需单独启动 log 守护进程。
"""

import sys
import os
import json
import asyncio
import argparse
import subprocess
import logging
from logging.handlers import RotatingFileHandler

# 复用 tmux_claude_log.py 的核心组件
from tmux_claude_log import ProjectWatcher, extract_message, project_dir_to_internal

try:
    import botpy
    from botpy.message import Message, DirectMessage
except ImportError:
    print("错误: 未安装 qq-botpy，请运行: pip install qq-botpy", file=sys.stderr)
    sys.exit(1)

# 最大回复长度（QQ 消息限制）
MAX_MESSAGE_LENGTH = 2000


def send_to_tmux(session, text):
    """发送文本到 tmux session"""
    escaped = text.replace("'", "'\\''")
    subprocess.run(
        ["tmux", "send-keys", "-t", session, escaped, "Enter"],
        capture_output=True,
    )


def setup_log_file(log_file):
    """设置 log 文件 handler"""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=100, encoding="utf-8"
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s"))
    return handler


class ClaudeBot(botpy.Client):
    """QQ Bot 客户端"""

    def __init__(self, session, watcher, log_handler, logger, **kwargs):
        super().__init__(**kwargs)
        self.session = session
        self.watcher = watcher  # ProjectWatcher 实例
        self.log_handler = log_handler  # log 文件 handler
        self._check_interval = 0.5
        self._external_logger = logger
        self._test_channel_id = None
        self._test_group_id = None
        self._test_c2c_openid = None
        self._config_path = None

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
        # 启动常驻监听协程
        asyncio.create_task(self._listen_forever())

    async def _listen_forever(self):
        """常驻监听协程：监听 JSONL → 写 log → 发送给用户"""
        self._external_logger.info("启动常驻监听协程")
        state = {}

        while True:
            try:
                for obj in self.watcher.poll(timeout=self._check_interval):
                    lines, _ = extract_message(obj, state)
                    for line in lines:
                        # 写入 log 文件
                        self.log_handler.emit(logging.LogRecord(
                            name="claude_log", level=logging.INFO,
                            pathname="", lineno=0, msg=line,
                            args=(), exc_info=None
                        ))
                        # 发送给用户
                        if line.startswith("[ASSISTANT]"):
                            text = line[len("[ASSISTANT]"):].strip()
                            if text:
                                await self._send_to_user(text)
                        elif line.startswith("[USER]"):
                            text = line[len("[USER]"):].strip()
                            if text:
                                await self._send_to_user(f"[用户] {text}")
                        elif line.startswith("[TOOL USE]"):
                            await self._send_to_user(f"[工具调用] {line[len('[TOOL USE]'):].strip()}")
                        elif line.startswith("[TOOL RESULT]"):
                            await self._send_to_user(f"[工具结果] {line[len('[TOOL RESULT]'):].strip()[:200]}...")
                        elif line.startswith("[TOOL ERROR]"):
                            await self._send_to_user(f"[工具错误] {line[len('[TOOL ERROR]'):].strip()[:200]}...")

                await asyncio.sleep(self._check_interval)
            except Exception as e:
                self._external_logger.error(f"监听协程错误: {e}")
                await asyncio.sleep(1)

    async def _send_to_user(self, text):
        """发送消息给配置的用户"""
        chunks = self._split_message(text)

        for chunk in chunks:
            try:
                if self._test_c2c_openid:
                    # 使用 api 发送 C2C 消息
                    await self.api.post_c2c_message(
                        openid=self._test_c2c_openid,
                        content=chunk
                    )
                    self._external_logger.info(f"已发送C2C消息 ({len(chunk)} 字符)")
                elif self._test_channel_id:
                    await self.api.post_message(
                        channel_id=self._test_channel_id,
                        content=chunk
                    )
                    self._external_logger.info(f"已发送频道消息 ({len(chunk)} 字符)")
                elif self._test_group_id:
                    await self.api.post_group_message(
                        group_openid=self._test_group_id,
                        content=chunk
                    )
                    self._external_logger.info(f"已发送群消息 ({len(chunk)} 字符)")
                await asyncio.sleep(0.5)
            except Exception as e:
                self._external_logger.error(f"发送消息失败: {e}")

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
        content = message.content
        if hasattr(message, "mentions") and message.mentions:
            for mention in message.mentions:
                content = content.replace(f"<@!{mention.id}>", "").strip()
        content = content.strip()
        if content:
            self._external_logger.info(f"发送到 tmux [{self.session}]: {content}")
            send_to_tmux(self.session, content)

    async def on_group_at_message_create(self, message: Message):
        """处理群 @ 消息"""
        self._external_logger.info(f"收到群消息: {message.content}")
        content = message.content
        if hasattr(message, "mentions") and message.mentions:
            for mention in message.mentions:
                content = content.replace(f"<@!{mention.id}>", "").strip()
        content = content.strip()
        if content:
            self._external_logger.info(f"发送到 tmux [{self.session}]: {content}")
            send_to_tmux(self.session, content)

    async def on_direct_message_create(self, message: DirectMessage):
        """处理私聊消息"""
        self._external_logger.info(f"收到私聊消息: {message.content}")
        content = message.content.strip()
        if content:
            self._external_logger.info(f"发送到 tmux [{self.session}]: {content}")
            send_to_tmux(self.session, content)

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
            self._save_openid_to_config(openid)

        content = message.content.strip()
        if content:
            self._external_logger.info(f"发送到 tmux [{self.session}]: {content}")
            send_to_tmux(self.session, content)

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
    test_channel_id = config.get("test_channel_id")
    test_group_id = config.get("test_group_id")
    test_c2c_openid = config.get("test_c2c_openid")

    if not appid or not secret:
        print("错误: 配置文件缺少 appid 或 secret", file=sys.stderr)
        sys.exit(1)

    # 检查 tmux session
    if not check_tmux_session(args.session):
        print(f"错误: tmux session '{args.session}' 不存在", file=sys.stderr)
        sys.exit(1)

    # 设置日志
    log_file = os.path.join(log_dir, "qq_bot.log")
    logger = setup_logging(log_file)

    # 设置 log 文件 handler (tmux_claude.log)
    claude_log_file = os.path.join(log_dir, "tmux_claude.log")
    log_handler = setup_log_file(claude_log_file)

    # 初始化 ProjectWatcher
    internal_dir = os.path.join(args.claude_dir, "projects", project_dir_to_internal(project_dir))
    if not os.path.isdir(internal_dir):
        print(f"错误: claude 数据目录不存在: {internal_dir}", file=sys.stderr)
        print("该项目可能尚未被 claude 打开过", file=sys.stderr)
        sys.exit(1)

    watcher = ProjectWatcher(internal_dir, skip_existing=True)

    print(f"[INFO] QQ Bot 启动: session={args.session}, log={log_file}", file=sys.stderr)
    print(f"[INFO] Claude log: {claude_log_file}", file=sys.stderr)

    # 创建 Bot
    intents = botpy.Intents(
        public_guild_messages=True,
        public_messages=True,
        direct_message=True,
    )

    client = ClaudeBot(
        session=args.session,
        watcher=watcher,
        log_handler=log_handler,
        logger=logger,
        intents=intents,
    )

    client.set_test_target(
        channel_id=test_channel_id,
        group_id=test_group_id,
        c2c_openid=test_c2c_openid,
        config_path=args.config,
    )

    # 运行 Bot
    try:
        client.run(appid=appid, secret=secret)
    finally:
        watcher.close()


if __name__ == "__main__":
    main()
