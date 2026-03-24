#!/usr/bin/env python3
"""
QQ Bot for tmux_claude - QQ 远程控制 Claude CLI

通过 QQ 消息与 Claude 交互：
- 接收 QQ 消息 → 发送到 tmux session
- 从 watch_loop 回调接收日志行 → 转发到 QQ

JSONL 解析、auto-approve、session 检查等逻辑全部在 tmux_claude_log.py 的 watch_loop 中。
"""

import sys
import os
import json
import time
import re
import base64
import asyncio
import threading
import subprocess
import logging
import shutil
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.request import urlopen

from tmux_claude_log import (
    ProjectWatcher, watch_loop, setup_logging as setup_claude_logging,
    project_dir_to_internal, send_approve, send_to_tmux,
    check_tmux_session, extract_message,
)

try:
    import botpy
    from botpy.http import Route
except ImportError:
    print("错误: 未安装 qq-botpy，请运行: pip install qq-botpy", file=sys.stderr)
    sys.exit(1)

# 最大回复长度（QQ 消息限制）
MAX_MESSAGE_LENGTH = 2000

# 媒体标签正则
MEDIA_TAG_RE = re.compile(r'<(qqimg|qqvoice|qqvideo|qqfile)>([^<>]+)</(?:qqimg|qqvoice|qqvideo|qqfile|img)>', re.IGNORECASE)

# 内部标记正则（如 [[reply_to: xxx]]）
INTERNAL_MARKER_RE = re.compile(r'\[\[[a-z_]+:\s*[^\]]*\]\]', re.IGNORECASE)

# 语音处理依赖检查
VOICE_DEPS_CHECKED = False
VOICE_DEPS_OK = False


def check_voice_deps():
    """检查语音处理依赖"""
    global VOICE_DEPS_CHECKED, VOICE_DEPS_OK
    if VOICE_DEPS_CHECKED:
        return VOICE_DEPS_OK

    VOICE_DEPS_CHECKED = True
    try:
        import pilk
        import speech_recognition
        if shutil.which('ffmpeg'):
            VOICE_DEPS_OK = True
            return True
    except ImportError:
        pass
    return False


def download_file(url, dest_path):
    """下载文件到指定路径"""
    with urlopen(url, timeout=30) as resp:
        with open(dest_path, 'wb') as f:
            f.write(resp.read())
    return dest_path


def silk_to_wav(amr_path, wav_path, logger=None):
    """SILK (AMR) 转 WAV"""
    try:
        import pilk
        pcm_path = amr_path.replace('.amr', '.pcm')
        pilk.decode(amr_path, pcm_path)
        if not os.path.exists(pcm_path):
            if logger:
                logger.error(f"pilk.decode 失败: {pcm_path} 不存在")
            return None

        result = subprocess.run([
            'ffmpeg', '-y', '-f', 's16le', '-ar', '24000', '-ac', '1',
            '-i', pcm_path, wav_path
        ], capture_output=True, text=True)

        if result.returncode != 0:
            if logger:
                logger.error(f"ffmpeg 失败: {result.stderr}")
            return None

        os.remove(pcm_path)
        return wav_path
    except Exception as e:
        if logger:
            logger.error(f"silk_to_wav 异常: {e}")
        return None


def transcribe_audio(wav_path, lang='zh-CN'):
    """语音转文字"""
    try:
        import speech_recognition as sr
        r = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = r.record(source)
        return r.recognize_google(audio, language=lang)
    except Exception as e:
        return None


def process_voice_attachment(url, filename, project_dir, logger):
    """处理语音附件：下载 → 转码 → 转文字
    返回: (text, local_path, status)
    status: 'success' | 'download_failed' | 'convert_failed' | 'transcribe_failed' | 'no_deps'
    """
    if not check_voice_deps():
        logger.warning("语音处理依赖不完整，跳过转文字")
        return None, None, 'no_deps'

    today = datetime.now().strftime('%Y%m%d')
    media_dir = os.path.join(project_dir, today)
    os.makedirs(media_dir, exist_ok=True)

    base_name = filename.rsplit('.', 1)[0]
    amr_path = os.path.join(media_dir, filename)
    wav_path = os.path.join(media_dir, base_name + '.wav')

    try:
        logger.info(f"下载语音: {url[:60]}...")
        download_file(url, amr_path)
        if not os.path.exists(amr_path):
            logger.error(f"下载失败: {amr_path} 不存在")
            return None, None, 'download_failed'

        logger.info(f"转换语音: {amr_path}")
        wav_result = silk_to_wav(amr_path, wav_path, logger)
        if not wav_result or not os.path.exists(wav_path):
            logger.error(f"转换失败: {wav_path} 不存在")
            return None, amr_path, 'convert_failed'

        logger.info(f"转文字: {wav_path}")
        text = transcribe_audio(wav_path)
        if text:
            logger.info(f"转写结果: {text}")
            return text, amr_path, 'success'
        else:
            logger.warning("转文字失败")
            return None, amr_path, 'transcribe_failed'
    except Exception as e:
        logger.error(f"语音处理失败: {e}")
        return None, None


def filter_internal_markers(text):
    """过滤内部标记，清理多余空行"""
    if not text:
        return text
    result = INTERNAL_MARKER_RE.sub('', text)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return result


class ClaudeBot(botpy.Client):
    """QQ Bot 客户端，只负责消息传递"""

    def __init__(self, session, queue, logger, project_dir=None, auto_approve=False, detail=False, **kwargs):
        super().__init__(**kwargs)
        self.session = session
        self._queue = queue
        self._external_logger = logger
        self._auto_approve = auto_approve
        self._detail = detail
        self._test_c2c_openid = None
        self._config_path = None
        self._project_dir = project_dir

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
        """消费 watch_loop 回调的日志行，转发到 QQ"""
        self._external_logger.info("启动消息转发协程")

        while True:
            try:
                line = await self._queue.get()

                if line.startswith("[ASSISTANT]"):
                    text = line[len("[ASSISTANT]"):].strip()
                    if text:
                        await self._send_to_user(text)
                elif line.startswith("[USER]"):
                    text = line[len("[USER]"):].strip()
                    if text:
                        if text == "/exit":
                            self._external_logger.info("收到 /exit 命令，退出")
                            return
                        await self._send_to_user(f"[用户] {text}")
                elif line.startswith("[TOOL USE]"):
                    if self._detail or not self._auto_approve:
                        await self._send_to_user(f"[工具调用] {line[len('[TOOL USE]'):].strip()}")
                    # 非 auto-approve 时，带 (waiting for approval) 的行提示用户
                    if not self._auto_approve and "(waiting for approval)" in line:
                        await self._send_to_user("[权限请求] 1=同意，其他到界面查看")
                elif line.startswith("[TOOL RESULT]"):
                    if self._detail:
                        await self._send_to_user(f"[工具结果] {line[len('[TOOL RESULT]'):].strip()[:200]}...")
                elif line.startswith("[TOOL ERROR]"):
                    if self._detail:
                        await self._send_to_user(f"[工具错误] {line[len('[TOOL ERROR]'):].strip()[:200]}...")
            except Exception as e:
                self._external_logger.error(f"消息转发错误: {e}")
                await asyncio.sleep(1)

    async def _send_to_user(self, text):
        """发送消息给配置的用户，支持媒体标签"""
        if not self._test_c2c_openid:
            return

        text = filter_internal_markers(text)
        send_queue = self._parse_media_tags(text)

        for item in send_queue:
            try:
                if item['type'] == 'text':
                    for chunk in self._split_message(item['content']):
                        await self.api.post_c2c_message(
                            openid=self._test_c2c_openid,
                            content=chunk
                        )
                        self._external_logger.info(f"已发送C2C消息 ({len(chunk)} 字符)")
                        await asyncio.sleep(0.5)
                elif item['type'] == 'image':
                    await self._send_image(item['content'])
                elif item['type'] == 'file':
                    await self._send_file(item['content'])
                elif item['type'] == 'voice':
                    await self._send_voice(item['content'])
                elif item['type'] == 'video':
                    await self._send_video(item['content'])
            except Exception as e:
                self._external_logger.error(f"发送消息失败: {e}")

    async def _send_image(self, path_or_url):
        """发送图片"""
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

        if not os.path.exists(path_or_url):
            await self.api.post_c2c_message(
                openid=self._test_c2c_openid,
                content=f"[图片文件不存在: {path_or_url}]"
            )
            return

        try:
            with open(path_or_url, 'rb') as f:
                file_data = base64.b64encode(f.read()).decode('utf-8')

            route = Route('POST', '/v2/users/{openid}/files', openid=self._test_c2c_openid)
            payload = {
                'file_type': 1,
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
                'file_type': 4,
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
                'file_type': 3,
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
                'file_type': 2,
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
            before = text[last_end:match.start()].strip()
            if before:
                queue.append({'type': 'text', 'content': before})

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

        after = text[last_end:].strip()
        if after:
            queue.append({'type': 'text', 'content': after})

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

        # 用户发送纯数字，发送方向键+Enter 确认权限选择
        if content.isdigit():
            n = int(content)
            self._external_logger.info(f"权限选择: {content}")
            for _ in range(n - 1):
                subprocess.run(["tmux", "send-keys", "-t", self.session, "Down"], capture_output=True)
                time.sleep(0.05)
            send_approve(self.session)
            return

        # 处理附件（图片、语音等）
        attachments_info = ""
        if hasattr(message, 'attachments') and message.attachments:
            try:
                atts = message.attachments
                if isinstance(atts, str):
                    atts = json.loads(atts)
                elif not isinstance(atts, list):
                    atts = list(atts) if hasattr(atts, '__iter__') else [atts]

                for att in atts:
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
                    elif content_type == 'voice' or content_type.startswith('audio/'):
                        text, local_path, status = process_voice_attachment(
                            url, filename, self._project_dir, self._external_logger
                        )
                        if status == 'success' and text:
                            attachments_info += f"\n[语音转文字]: {text}\n"
                        elif status == 'convert_failed':
                            attachments_info += f"\n[转码失败]\n"
                        elif status == 'transcribe_failed':
                            attachments_info += f"\n[语音太短无法识别]\n"
                        else:
                            attachments_info += f"\n[语音: {filename}]\n语音URL: {url}\n"
                    elif content_type.startswith('video/'):
                        attachments_info += f"\n[视频: {filename}]\n视频URL: {url}\n"
                    else:
                        attachments_info += f"\n[附件: {filename}]\nURL: {url}\n"
            except Exception as e:
                self._external_logger.error(f"解析附件失败: {e}")

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


def run_qq_bot(session, project_dir, log_dir, internal_dir, qq_config,
               auto_approve=False, load_md=False, detail=False, extract_fn=extract_message):
    """启动 QQ Bot，供 tmux_claude_log.py 调用"""
    project_dir = os.path.abspath(project_dir)
    log_dir = os.path.abspath(log_dir)

    appid = qq_config.get("appid")
    secret = qq_config.get("secret")
    test_c2c_openid = qq_config.get("test_c2c_openid")

    if not appid or not secret:
        print("错误: 配置缺少 appid 或 secret", file=sys.stderr)
        sys.exit(1)

    if not check_tmux_session(session):
        print(f"错误: tmux session '{session}' 不存在", file=sys.stderr)
        sys.exit(1)

    log_file = os.path.join(log_dir, "qq_bot.log")
    logger = setup_logging(log_file)

    # claude_log 写入 tmux_claude.log
    claude_log_file = os.path.join(log_dir, "tmux_claude.log")
    claude_logger = setup_claude_logging(claude_log_file)

    print(f"[INFO] QQ Bot 启动: session={session}, log={log_file}", file=sys.stderr)

    # 等待 claude 数据目录就绪
    if not os.path.isdir(internal_dir):
        print(f"[INFO] 等待 claude 数据目录: {internal_dir}", file=sys.stderr)
        while not os.path.isdir(internal_dir):
            time.sleep(1)
        print(f"[INFO] claude 数据目录已就绪", file=sys.stderr)

    watcher = ProjectWatcher(internal_dir, skip_existing=True)
    stop_event = {"stop": False}
    queue = asyncio.Queue()

    intents = botpy.Intents(
        public_messages=True,
        direct_message=True,
    )

    client = ClaudeBot(
        session=session,
        queue=queue,
        logger=logger,
        project_dir=project_dir,
        auto_approve=auto_approve,
        detail=detail,
        intents=intents,
    )

    client.set_test_target(c2c_openid=test_c2c_openid)

    # watch_loop 在独立线程中运行，通过 on_line 回调往 queue 放数据
    def on_line(line):
        loop = client.loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(queue.put_nowait, line)

    def run_watcher():
        watch_loop(watcher, claude_logger, session, stop_event, auto_approve,
                   extract_fn, load_md, on_line)

    watcher_thread = threading.Thread(target=run_watcher, daemon=True)
    watcher_thread.start()

    try:
        client.run(appid=appid, secret=secret)
    finally:
        stop_event["stop"] = True
        watcher.close()
