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
import time
import re
import base64
import asyncio
import argparse
import subprocess
import logging
from logging.handlers import RotatingFileHandler

# 复用 tmux_claude_log.py 的核心组件
from tmux_claude_log import ProjectWatcher, extract_message, project_dir_to_internal, send_approve

try:
    import botpy
    from botpy.http import Route
except ImportError:
    print("错误: 未安装 qq-botpy，请运行: pip install qq-botpy", file=sys.stderr)
    sys.exit(1)

# 最大回复长度（QQ 消息限制）
MAX_MESSAGE_LENGTH = 2000

# tmux session 检查间隔
SESSION_CHECK_INTERVAL = 10.0

# 媒体标签正则
MEDIA_TAG_RE = re.compile(r'<(qqimg|qqvoice|qqvideo|qqfile)>([^<>]+)</(?:qqimg|qqvoice|qqvideo|qqfile|img)>', re.IGNORECASE)

# 内部标记正则（如 [[reply_to: xxx]]）
INTERNAL_MARKER_RE = re.compile(r'\[\[[a-z_]+:\s*[^\]]*\]\]', re.IGNORECASE)


def filter_internal_markers(text):
    """过滤内部标记，清理多余空行"""
    if not text:
        return text
    result = INTERNAL_MARKER_RE.sub('', text)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return result


def send_to_tmux(session, text):
    """发送文本到 tmux session"""
    subprocess.run(
        ["tmux", "send-keys", "-t", session, text, "Enter"],
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

    def __init__(self, session, watcher, log_handler, logger, auto_approve=False, **kwargs):
        super().__init__(**kwargs)
        self.session = session
        self.watcher = watcher
        self.log_handler = log_handler
        self._check_interval = 0.5
        self._external_logger = logger
        self._auto_approve = auto_approve
        self._test_c2c_openid = None
        self._config_path = None

    def set_test_target(self, c2c_openid=None, config_path=None):
        """设置测试消息目标"""
        self._test_c2c_openid = c2c_openid
        self._config_path = config_path

    async def on_ready(self):
        """Bot 就绪"""
        self._external_logger.info(f"Claude Bot 已就绪，session: {self.session}")
        await self._send_online_notification()
        asyncio.create_task(self._listen_forever())

    async def _listen_forever(self):
        """常驻监听协程：监听 JSONL → 写 log → 发送给用户"""
        self._external_logger.info("启动常驻监听协程")
        state = {}
        last_session_check = time.monotonic()

        while True:
            try:
                for obj in self.watcher.poll(timeout=self._check_interval):
                    lines, needs_approve = extract_message(obj, state)
                    for line in lines:
                        self.log_handler.emit(logging.LogRecord(
                            name="claude_log", level=logging.INFO,
                            pathname="", lineno=0, msg=line,
                            args=(), exc_info=None
                        ))
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

                    if needs_approve:
                        if self._auto_approve:
                            send_approve(self.session)
                            self._external_logger.info("[tmux_claude auto approve]")
                        else:
                            # 通知用户可以发送数字选择
                            await self._send_to_user("[权限请求] 请回复数字选择（如 1、2、3）")

                now = time.monotonic()
                if now - last_session_check >= SESSION_CHECK_INTERVAL:
                    last_session_check = now
                    if not check_tmux_session(self.session):
                        self._external_logger.info(f"tmux session '{self.session}' 已结束，退出")
                        break

                await asyncio.sleep(self._check_interval)
            except Exception as e:
                self._external_logger.error(f"监听协程错误: {e}")
                await asyncio.sleep(1)

    async def _send_to_user(self, text):
        """发送消息给配置的用户，支持媒体标签"""
        if not self._test_c2c_openid:
            return

        # 过滤内部标记
        text = filter_internal_markers(text)

        # 解析媒体标签，构建发送队列
        send_queue = self._parse_media_tags(text)

        for item in send_queue:
            try:
                if item['type'] == 'text':
                    # 发送纯文本
                    for chunk in self._split_message(item['content']):
                        await self.api.post_c2c_message(
                            openid=self._test_c2c_openid,
                            content=chunk
                        )
                        self._external_logger.info(f"已发送C2C消息 ({len(chunk)} 字符)")
                        await asyncio.sleep(0.5)
                elif item['type'] == 'image':
                    # 发送图片
                    await self._send_image(item['content'])
                elif item['type'] == 'file':
                    # 发送文件
                    await self._send_file(item['content'])
                elif item['type'] == 'voice':
                    # 发送语音
                    await self._send_voice(item['content'])
                elif item['type'] == 'video':
                    # 发送视频
                    await self._send_video(item['content'])
            except Exception as e:
                self._external_logger.error(f"发送消息失败: {e}")

    async def _send_image(self, path_or_url):
        """发送图片"""
        # 公网 URL，直接使用 API
        if path_or_url.startswith(('http://', 'https://')):
            try:
                media = await self.api.post_c2c_file(
                    openid=self._test_c2c_openid,
                    file_type=1,
                    url=path_or_url,
                    srv_send_msg=True
                )
                self._external_logger.info(f"已发送图片(URL): {path_or_url}")
                await asyncio.sleep(0.5)
                return
            except Exception as e:
                self._external_logger.error(f"发送图片URL失败: {e}")
                await self.api.post_c2c_message(
                    openid=self._test_c2c_openid,
                    content=f"[图片发送失败: {path_or_url}]"
                )
                return

        # 本地文件，读取并上传 Base64
        if not os.path.exists(path_or_url):
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[图片文件不存在: {path_or_url}]"
            )
            return

        try:
            # 读取文件并转为 Base64
            with open(path_or_url, 'rb') as f:
                file_data = base64.b64encode(f.read()).decode('utf-8')

            # 直接调用 HTTP API 上传
            route = Route('POST', '/v2/users/{openid}/files', openid=self._test_c2c_openid)
            payload = {
                'file_type': 1,  # 图片
                'file_data': file_data,
                'srv_send_msg': True
            }
            result = await self.api._http.request(route, json=payload)
            self._external_logger.info(f"已发送图片(Base64): {path_or_url}")
            await asyncio.sleep(0.5)
        except Exception as e:
            self._external_logger.error(f"发送图片失败: {e}")
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[图片发送失败: {path_or_url}]"
            )

    async def _send_file(self, path_or_url):
        """发送文件"""
        # 公网 URL
        if path_or_url.startswith(('http://', 'https://')):
            try:
                media = await self.api.post_c2c_file(
                    openid=self._test_c2c_openid,
                    file_type=4,
                    url=path_or_url,
                    srv_send_msg=True
                )
                self._external_logger.info(f"已发送文件(URL): {path_or_url}")
                await asyncio.sleep(0.5)
                return
            except Exception as e:
                self._external_logger.error(f"发送文件URL失败: {e}")
                await self.api.post_c2c_message(
                    openid=self._test_c2c_openid,
                    content=f"[文件发送失败: {path_or_url}]"
                )
                return

        # 本地文件
        if not os.path.exists(path_or_url):
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[文件不存在: {path_or_url}]"
            )
            return

        try:
            with open(path_or_url, 'rb') as f:
                file_data = base64.b64encode(f.read()).decode('utf-8')

            filename = os.path.basename(path_or_url)
            route = Route('POST', '/v2/users/{openid}/files', openid=self._test_c2c_openid)
            payload = {
                'file_type': 4,  # 文件
                'file_data': file_data,
                'srv_send_msg': False,
                'filename': filename
            }
            result = await self.api._http.request(route, json=payload)
            file_info = result.get('file_info')
            if file_info:
                await self.api.post_c2c_message(
                    openid=self._test_c2c_openid,
                    msg_type=7,
                    media={'file_info': file_info, 'name': filename},
                    content=f'📄 {filename}'
                )
                self._external_logger.info(f"已发送文件: {path_or_url}")
            else:
                self._external_logger.error(f"上传文件未返回 file_info: {result}")
            await asyncio.sleep(0.5)
        except Exception as e:
            self._external_logger.error(f"发送文件失败: {e}")
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[文件发送失败: {path_or_url}]"
            )

    async def _send_voice(self, path):
        """发送语音"""
        if path.startswith(('http://', 'https://')):
            try:
                media = await self.api.post_c2c_file(
                    openid=self._test_c2c_openid,
                    file_type=3,
                    url=path,
                    srv_send_msg=True
                )
                self._external_logger.info(f"已发送语音(URL): {path}")
                await asyncio.sleep(0.5)
                return
            except Exception as e:
                self._external_logger.error(f"发送语音URL失败: {e}")
                await self.api.post_c2c_message(
                    openid=self._test_c2c_openid,
                    content=f"[语音发送失败: {path}]"
                )
                return

        if not os.path.exists(path):
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[语音文件不存在: {path}]"
            )
            return

        try:
            with open(path, 'rb') as f:
                file_data = base64.b64encode(f.read()).decode('utf-8')

            route = Route('POST', '/v2/users/{openid}/files', openid=self._test_c2c_openid)
            payload = {
                'file_type': 3,  # 语音
                'file_data': file_data,
                'srv_send_msg': True
            }
            result = await self.api._http.request(route, json=payload)
            self._external_logger.info(f"已发送语音(Base64): {path}")
            await asyncio.sleep(0.5)
        except Exception as e:
            self._external_logger.error(f"发送语音失败: {e}")
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[语音发送失败: {path}]"
            )

    async def _send_video(self, path_or_url):
        """发送视频"""
        if path_or_url.startswith(('http://', 'https://')):
            try:
                media = await self.api.post_c2c_file(
                    openid=self._test_c2c_openid,
                    file_type=2,
                    url=path_or_url,
                    srv_send_msg=True
                )
                self._external_logger.info(f"已发送视频(URL): {path_or_url}")
                await asyncio.sleep(0.5)
                return
            except Exception as e:
                self._external_logger.error(f"发送视频URL失败: {e}")
                await self.api.post_c2c_message(
                    openid=self._test_c2c_openid,
                    content=f"[视频发送失败: {path_or_url}]"
                )
                return

        if not os.path.exists(path_or_url):
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[视频文件不存在: {path_or_url}]"
            )
            return

        try:
            with open(path_or_url, 'rb') as f:
                file_data = base64.b64encode(f.read()).decode('utf-8')

            route = Route('POST', '/v2/users/{openid}/files', openid=self._test_c2c_openid)
            payload = {
                'file_type': 2,  # 视频
                'file_data': file_data,
                'srv_send_msg': True
            }
            result = await self.api._http.request(route, json=payload)
            self._external_logger.info(f"已发送视频(Base64): {path_or_url}")
            await asyncio.sleep(0.5)
        except Exception as e:
            self._external_logger.error(f"发送视频失败: {e}")
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[视频发送失败: {path_or_url}]"
            )

    def _parse_media_tags(self, text):
        """解析媒体标签，返回发送队列"""
        queue = []
        last_end = 0

        for match in MEDIA_TAG_RE.finditer(text):
            # 添加标签前的文本
            before = text[last_end:match.start()].strip()
            if before:
                queue.append({'type': 'text', 'content': before})

            # 添加媒体项
            tag_type = match.group(1).lower()
            content = match.group(2).strip()
            if tag_type == 'qqimg':
                queue.append({'type': 'image', 'content': content})
            elif tag_type == 'qqfile':
                queue.append({'type': 'file', 'content': content})
            elif tag_type == 'qqvoice':
                queue.append({'type': 'voice', 'content': content})
            elif tag_type == 'qqvideo':
                queue.append({'type': 'video', 'content': content})

            last_end = match.end()

        # 添加最后一个标签后的文本
        after = text[last_end:].strip()
        if after:
            queue.append({'type': 'text', 'content': after})

        # 如果没有媒体标签，返回整个文本
        if not queue:
            queue.append({'type': 'text', 'content': text})

        return queue

    async def _send_online_notification(self):
        """发送上线通知到用户"""
        if not self._test_c2c_openid:
            return

        await asyncio.sleep(1)
        msg = f"🤖 Claude Bot 已上线！session: {self.session}"

        try:
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=msg
            )
            self._external_logger.info(f"已发送上线通知到用户: {self._test_c2c_openid}")
        except Exception as e:
            self._external_logger.error(f"发送C2C消息失败: {e}")

    async def on_c2c_message_create(self, message):
        """处理 C2C 单聊消息"""
        self._external_logger.info(f"收到C2C消息: {message}")

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

        # 用户发送纯数字，发送 Enter 确认权限选择
        if content.isdigit():
            self._external_logger.info(f"权限选择: {content}")
            send_approve(self.session)
            return

        # 处理附件（图片等）
        attachments_info = ""
        if hasattr(message, 'attachments') and message.attachments:
            try:
                # attachments 可能是字符串、列表或对象
                atts = message.attachments
                if isinstance(atts, str):
                    atts = json.loads(atts)
                elif not isinstance(atts, list):
                    # 可能是单个对象或可迭代对象
                    atts = list(atts) if hasattr(atts, '__iter__') else [atts]

                for att in atts:
                    # 支持 dict 和对象两种形式
                    if isinstance(att, dict):
                        content_type = att.get('content_type', '')
                        url = att.get('url', '')
                        filename = att.get('filename', '')
                    else:
                        content_type = getattr(att, 'content_type', '') or ''
                        url = getattr(att, 'url', '') or ''
                        filename = getattr(att, 'filename', '') or ''

                    if content_type.startswith('image/'):
                        attachments_info += f"\n[图片: {filename}]\n图片URL: {url}\n"
                        self._external_logger.info(f"收到图片附件: {filename}")
                    elif content_type.startswith('audio/'):
                        attachments_info += f"\n[语音: {filename}]\n语音URL: {url}\n"
                    elif content_type.startswith('video/'):
                        attachments_info += f"\n[视频: {filename}]\n视频URL: {url}\n"
                    else:
                        attachments_info += f"\n[附件: {filename}]\nURL: {url}\n"
            except Exception as e:
                self._external_logger.error(f"解析附件失败: {e}")

        # 合并文本和附件信息
        full_content = content + attachments_info
        if full_content.strip():
            self._external_logger.info(f"发送到 tmux [{self.session}]: {full_content[:100]}...")
            send_to_tmux(self.session, full_content)

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

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(console_handler)

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
    parser.add_argument("--auto-approve", action="store_true", help="自动确认所有权限请求")
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)
    log_dir = os.path.abspath(args.log_dir)

    if not os.path.exists(args.config):
        print(f"错误: 配置文件不存在: {args.config}", file=sys.stderr)
        sys.exit(1)

    with open(args.config, "r") as f:
        config = json.load(f)

    appid = config.get("appid")
    secret = config.get("secret")
    test_c2c_openid = config.get("test_c2c_openid")

    if not appid or not secret:
        print("错误: 配置文件缺少 appid 或 secret", file=sys.stderr)
        sys.exit(1)

    if not check_tmux_session(args.session):
        print(f"错误: tmux session '{args.session}' 不存在", file=sys.stderr)
        sys.exit(1)

    log_file = os.path.join(log_dir, "qq_bot.log")
    logger = setup_logging(log_file)

    claude_log_file = os.path.join(log_dir, "tmux_claude.log")
    log_handler = setup_log_file(claude_log_file)

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
        public_messages=True,
        direct_message=True,
    )

    client = ClaudeBot(
        session=args.session,
        watcher=watcher,
        log_handler=log_handler,
        logger=logger,
        auto_approve=args.auto_approve,
        intents=intents,
    )

    client.set_test_target(
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
