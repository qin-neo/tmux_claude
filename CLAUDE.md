# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A tmux-based wrapper for managing multiple Claude CLI processes in the background, with automatic output logging.

## Components

1. **tmux_claude.sh** — Session manager. No args lists active sessions; with a directory arg, creates a tmux session (named after the directory basename), cd's into that directory, and runs `claude --continue` (falls back to `claude` if no prior session). Automatically starts the log daemon or QQ Bot. Supports `stop` command to tear down session and log daemon.
2. **tmux_claude_log.py** — Pure background daemon (launched by `tmux_claude.sh` when no QQ config). Monitors `~/.claude/projects/<project>/` JSONL files for changes → extracts user messages, assistant text, and tool-use summaries → writes to `RotatingFileHandler` (10MB per file, 100 backups). Exits gracefully on SIGTERM/SIGINT or when the tmux session ends.
3. **qq_bot.py** — QQ Bot client (launched when `qq_bot_config.json` exists in project dir). Imports `ProjectWatcher` and `extract_message` from `tmux_claude_log.py`. Monitors JSONL files → writes log + sends Claude replies to QQ. Only C2C messaging is verified.

## Running

```bash
# List active tmux sessions (or show help if none)
./tmux_claude.sh

# Start a new session in a project directory (auto-attaches)
./tmux_claude.sh /path/to/project

# Start with auto-approve mode (all_yes)
./tmux_claude.sh /path/to/project all_yes

# Start in background mode (no auto-attach)
./tmux_claude.sh /path/to/project --daemon

# Combine: auto-approve + background mode
./tmux_claude.sh /path/to/project all_yes --daemon

# Specify custom claude command
./tmux_claude.sh /path/to/project --claude "claude"

# Stop a session and its log daemon
./tmux_claude.sh /path/to/project stop
```

## File Conventions

Given session name `<name>` and directory `<dir>`:
- JSONL source: `~/.claude/projects/<dir_with_slashes_replaced_by_dashes>/`
- Log file: `<dir>/tmux_claude.log` (+ `.1` .. `.100` rotated backups)

## Dependencies

- **tmux** (system package)
- **Python 3** (standard library only; no third-party packages required)

## Architecture

**tmux_claude.sh** determines `SCRIPT_DIR` to locate `tmux_claude_log.py` relative to itself. Session name = `basename` of the directory argument (with `.` and `:` replaced by `_`). Sets tmux options for 256-color, true color, mouse support, and UTF-8 locale.

**tmux_claude_log.py** maps the project directory to `~/.claude/projects/` (e.g. `/root/foo` → `-root-foo`), then polls all `*.jsonl` files (including `subagents/`) for new lines. Each JSONL line is parsed: `user` messages log content as `[USER]`, `assistant` text blocks as `[ASSISTANT]`, tool-use blocks as `[TOOL USE] <name>: <summary>`, and tool results as `[TOOL result]`. Meta messages (`isMeta: true`) and `file-history-snapshot` / `progress` types are skipped.

## Code Style

- **DRY** - Extract repeated logic into functions
- **Format strings must match inputs**
- **Concise comments** - Single-line for internal functions; skip obvious comments; only comment implicit constraints or non-self-explanatory names
- **Code and comments must stay in sync**

## Notes

### SSH Remote Execution

Non-interactive shell when using `ssh host "command"` - `~/.bashrc` not loaded.

**Solution**: `ssh host "source ~/.bashrc && command"` or `ssh host "bash -i -c 'command'"`

**Don't**: Load nvm/bashrc in scripts. Scripts run in correct environment.

### Deployment

Confirm before push to github when user says "deploy".

部署到 hk2：
```bash
# 同步文件
scp tmux_claude.sh tmux_claude_log.py qq_bot.py hk2:~/tmux_claude/

# 停止旧会话
ssh hk2 "tmux kill-session -t war 2>/dev/null || true"

# 启动（必须带 all_yes，用 bash -i -c 加载 nvm 环境）
ssh hk2 'bash -i -c "tmux_claude war all_yes --daemon"'
```
