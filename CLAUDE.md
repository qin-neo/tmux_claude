# CLAUDE.md

tmux wrapper for managing Claude CLI processes with automatic logging.

## Components

- **tmux_claude.sh** — Session manager (create/stop tmux sessions)
- **tmux_claude_log.py** — Log daemon (monitors JSONL → writes to rotating log)
- **qq_bot.py** — QQ Bot module (loaded by tmux_claude_log.py when configured)

## Usage

```bash
./tmux_claude.sh /path/to/project        # Start session (auto-attach)
./tmux_claude.sh /path/to/project --daemon # Background mode
./tmux_claude.sh /path/to/project stop   # Stop session
```

## Config: tmux_claude.json

Place in project directory:

```json
{
  "auto_approve": false,
  "load_md": false,
  "detail": false,
  "qq_bot": {
    "appid": "...",
    "secret": "...",
    "test_c2c_openid": "..."
  }
}
```

- `auto_approve`: Auto-approve all permission requests
- `load_md`: Read CLAUDE.md on startup
- `detail`: Send tool results to QQ (QQ Bot only)
- `qq_bot`: QQ Bot config (omit to disable)

## QQ Bot Message Options

| auto_approve | detail | Tool Use | Tool Result |
|--------------|--------|----------|-------------|
| false | false | ✓ | ✗ |
| true | false | ✗ | ✗ |
| false | true | ✓ | ✓ |
| true | true | ✓ | ✓ |

## Files

- JSONL: `~/.claude/projects/<dir_with_dashes>/`
- Log: `<dir>/tmux_claude.log` (10MB × 100 backups)

## Code Style

- DRY
- Format strings must match inputs
- Concise comments (only for non-obvious logic)
- 有疑问就问，不允许自作主张

## Projects (hk2)

| 项目 | 路径 | 说明 |
|------|------|------|
| war | ~/war | 部署脚本 |
| todo-list | ~/todo-list | 语音待办清单助手 |

## Deployment

Confirm before push to github when user says "deploy".

```bash
scp tmux_claude.sh tmux_claude_log.py qq_bot.py hk2:~/tmux_claude/
ssh hk2 'bash -i -c "tmux_claude war --daemon"'
```
