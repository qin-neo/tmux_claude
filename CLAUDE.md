# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A tmux-based wrapper for managing multiple Claude CLI processes in the background, with automatic output logging.

## Components

1. **tmux_claude.sh** — Session manager. No args lists active sessions; with a directory arg, creates a tmux session (named after the directory basename), cd's into that directory, and runs `claude --continue` (falls back to `claude` if no prior session). Automatically starts the log daemon. Supports `stop` command to tear down session and log daemon.
2. **tmux_claude_log.py** — Pure background daemon (launched by `tmux_claude.sh`, not called directly). Monitors `~/.claude/projects/<project>/` JSONL files for changes → extracts user messages, assistant text, and tool-use summaries → writes to `RotatingFileHandler` (10MB per file, 100 backups). Writes a PID file for liveness detection. Exits gracefully on SIGTERM/SIGINT or when the tmux session ends.

## Running

```bash
# List active tmux sessions (or show help if none)
./tmux_claude.sh

# Start a new session in a project directory (auto-attaches)
./tmux_claude.sh /path/to/project

# Stop a session and its log daemon
./tmux_claude.sh /path/to/project stop
```

## File Conventions

Given session name `<name>` and directory `<dir>`:
- JSONL source: `~/.claude/projects/<dir_with_slashes_replaced_by_dashes>/`
- Log file: `<dir>/tmux_claude.log` (+ `.1` .. `.100` rotated backups)
- PID file: `<dir>/tmux_claude.pid`

## Dependencies

- **tmux** (system package)
- **Python 3** (standard library only; no third-party packages required)

## Architecture

**tmux_claude.sh** determines `SCRIPT_DIR` to locate `tmux_claude_log.py` relative to itself. Session name = `basename` of the directory argument (with `.` and `:` replaced by `_`). Sets tmux options for 256-color, true color, mouse support, and UTF-8 locale.

**tmux_claude_log.py** maps the project directory to `~/.claude/projects/` (e.g. `/root/foo` → `-root-foo`), then polls all `*.jsonl` files (including `subagents/`) for new lines. Each JSONL line is parsed: `user` messages log content as `[USER]`, `assistant` text blocks as `[ASSISTANT]`, tool-use blocks as `[TOOL USE] <name>: <summary>`, and tool results as `[TOOL result]`. Meta messages (`isMeta: true`) and `file-history-snapshot` / `progress` types are skipped. Uses 2-second polling interval.
