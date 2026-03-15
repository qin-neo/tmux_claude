# tmux_claude

基于 tmux 的 Claude CLI 会话管理器，支持后台运行与自动日志记录。

## 功能特性

- 在 tmux 会话中启动 Claude CLI，支持后台运行
- 自动监控并记录对话日志到文件
- 支持自动确认权限请求（all_yes 模式）
- 日志文件自动轮转（单文件 10MB，最多保留 100 个备份）
- 会话结束后自动退出守护进程
- **QQ Bot 集成** - 通过 QQ 消息远程控制 Claude

## 依赖

- **tmux** - 终端复用器
- **Python 3** - 仅使用标准库（QQ Bot 需额外安装 qq-botpy）
- **Claude CLI** - `npm install -g @anthropic-ai/claude-code`
- **qq-botpy**（可选）- `pip install qq-botpy`

## 安装

```bash
# 克隆仓库
git clone https://github.com/yourname/tmux_claude.git
cd tmux_claude

# 添加到 PATH（可选）
ln -s $(pwd)/tmux_claude.sh /usr/local/bin/tmux_claude
```

## 使用方法

```bash
# 列出当前活动的 tmux 会话
./tmux_claude.sh

# 在指定目录启动 Claude 会话（自动附加到 tmux）
./tmux_claude.sh /path/to/project

# 启动并自动确认所有权限请求
./tmux_claude.sh /path/to/project all_yes

# 停止会话及其日志守护进程
./tmux_claude.sh /path/to/project stop

# 后台模式启动（不附加到 tmux）
./tmux_claude.sh /path/to/project --daemon

# 组合：自动确认 + 后台模式
./tmux_claude.sh /path/to/project all_yes --daemon
```

### QQ Bot 远程控制（实验性）

在项目目录下创建 `qq_bot_config.json`：

```json
{
    "appid": "YOUR_APP_ID",
    "secret": "YOUR_BOT_SECRET",
    "test_c2c_openid": null
}
```

启动时检测到配置文件会自动启动 QQ Bot。目前仅验证了 C2C 单聊功能：发送消息到 QQ 即可远程控制 Claude，Claude 的回复会自动发送回 QQ。

首次使用时，`test_c2c_openid` 可留空，Bot 会在收到第一条 C2C 消息时自动记录发送者的 openid。

## 文件说明

| 文件 | 说明 |
|------|------|
| `tmux_claude.sh` | 会话管理脚本 |
| `tmux_claude_log.py` | 日志守护进程（无 QQ 配置时使用） |
| `qq_bot.py` | QQ Bot 客户端（有 QQ 配置时使用） |
| `qq_bot_config.json.example` | QQ Bot 配置模板 |

## 日志文件位置

对于项目目录 `<dir>`：

- **JSONL 数据源**: `~/.claude/projects/<dir_with_slashes_replaced_by_dashes>/`
- **日志文件**: `<dir>/tmux_claude.log`（以及 `.1` 到 `.100` 轮转备份）

例如，项目目录为 `/root/myproject`，则：
- JSONL 源目录: `~/.claude/projects/-root-myproject/`
- 日志文件: `/root/myproject/tmux_claude.log`

## 日志格式示例

```
2025-03-15 10:30:00 INFO [USER] 帮我写一个 Python 脚本
2025-03-15 10:30:05 INFO [ASSISTANT] 好的，我来帮你创建一个脚本...
2025-03-15 10:30:10 INFO [TOOL USE] Write: /root/myproject/script.py
2025-03-15 10:30:12 INFO [TOOL RESULT]
文件已成功创建
```

## 工作原理

### tmux_claude.sh

1. 检测 `claude` 命令是否可用
2. 以目录名作为 tmux 会话名创建会话
3. 在会话中执行 `claude --continue`（无历史会话则执行 `claude`）
4. 启动日志守护进程监控对话
5. 附加到 tmux 会话

### tmux_claude_log.py

1. 使用 Linux inotify 监控 Claude 项目目录下的 JSONL 文件变化
2. 解析新增的 JSONL 行，提取用户消息、助手回复、工具调用等信息
3. 写入轮转日志文件
4. 定期检查 tmux 会话是否存在，会话结束后自动退出

## 快捷键

在 tmux 会话中：

- `Ctrl+B D` - 分离会话（会话继续后台运行）
- `Ctrl+B [` - 进入复制模式（可滚动查看历史输出）

## 许可证

MIT License
