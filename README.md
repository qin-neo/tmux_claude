# tmux_claude

基于 tmux 的 Claude CLI 会话管理器，支持后台运行与自动日志记录。

## 功能特性

- 在 tmux 会话中启动 Claude CLI，支持后台运行
- 自动监控并记录对话日志到文件
- 支持自动确认权限请求
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

# 后台模式启动（不附加到 tmux）
./tmux_claude.sh /path/to/project --daemon

# 自动确认所有权限请求
./tmux_claude.sh /path/to/project --all-yes

# 指定自定义 claude 启动命令
./tmux_claude.sh /path/to/project --claude "my-cli"

# 重启会话
./tmux_claude.sh /path/to/project restart

# 停止会话
./tmux_claude.sh /path/to/project stop
```

### 命令行选项

| 选项 | 说明 |
|------|------|
| `--daemon` | 后台启动，不附加到 tmux |
| `--all-yes` | 自动确认所有权限请求（覆盖配置文件） |
| `--claude CMD` | 指定 claude 命令（默认: `claude --effort max`） |

**CLAUDE_DIR 推断**：根据 `--claude` 参数推断数据目录
- `claude` → `~/.claude`
- `my-cli` → `~/.my-cli`

## 配置文件

在项目目录下创建 `tmux_claude.json`：

```json
{
  "auto_approve": false,
  "load_md": false,
  "detail": false,
  "qq_bot": {
    "appid": "YOUR_APP_ID",
    "secret": "YOUR_BOT_SECRET",
    "test_c2c_openid": null
  }
}
```

### 配置项说明

| 字段 | 说明 |
|------|------|
| `auto_approve` | 自动确认所有权限请求 |
| `load_md` | 启动时读取 CLAUDE.md |
| `detail` | 发送工具结果到 QQ（仅 QQ Bot） |
| `qq_bot` | QQ Bot 配置，省略则禁用 QQ Bot |

### QQ Bot 消息选项

| auto_approve | detail | 工具调用 | 工具结果 |
|--------------|--------|----------|----------|
| false | false | ✓ | ✗ |
| true | false | ✗ | ✗ |
| false | true | ✓ | ✓ |
| true | true | ✓ | ✓ |

首次使用 QQ Bot 时，`test_c2c_openid` 可留空，Bot 会在收到第一条 C2C 消息时自动记录发送者的 openid。

## 文件说明

| 文件 | 说明 |
|------|------|
| `tmux_claude.sh` | 会话管理脚本 |
| `tmux_claude_log.py` | 日志守护进程（主入口） |
| `qq_bot.py` | QQ Bot 模块（被 tmux_claude_log.py 调用） |

## 日志文件位置

对于项目目录 `<dir>`：

- **JSONL 数据源**: `<CLAUDE_DIR>/projects/<dir_with_slashes_replaced_by_dashes>/`
- **日志文件**: `<dir>/tmux_claude.log`（以及 `.1` 到 `.100` 轮转备份）

例如，项目目录为 `/root/myproject`，使用默认 `claude` 命令：
- JSONL 源目录: `~/.claude/projects/-root-myproject/`
- 日志文件: `/root/myproject/tmux_claude.log`

使用 `--claude my-cli` 时：
- JSONL 源目录: `~/.my-cli/projects/-root-myproject/`

## 日志格式示例

```
2025-03-15 10:30:00 INFO [USER] 帮我写一个 Python 脚本
2025-03-15 10:30:05 INFO [ASSISTANT] 好的，我来帮你创建一个脚本...
2025-03-15 10:30:10 INFO [TOOL USE] Write: /root/myproject/script.py
2025-03-15 10:30:12 INFO [TOOL RESULT]
文件已成功创建
```

## 快捷键

在 tmux 会话中：

- `Ctrl+B D` - 分离会话（会话继续后台运行）
- `Ctrl+B [` - 进入复制模式（可滚动查看历史输出）

## 许可证

MIT License
