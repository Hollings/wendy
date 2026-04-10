#!/bin/bash
# PreToolUse hook: blocks Write/Edit to protected paths.
#
# Protected: CLAUDE.md, /app/config/, /root/.claude/,
#            /data/wendy/claude_fragments/ (except people/)
# Allowed:   channel workspaces, journals, people fragments, /tmp/

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[ -z "$FILE_PATH" ] && exit 0

block() {
  jq -n --arg reason "$1" '{decision: "block", reason: $reason}'
  exit 0
}

# Allow: people fragments (before blocking the parent dir)
case "$FILE_PATH" in
  /data/wendy/claude_fragments/people/*) exit 0 ;;
esac

# Block: core fragments
case "$FILE_PATH" in
  /data/wendy/claude_fragments/*) block "Fragment files are protected." ;;
esac

# Block: CLAUDE.md anywhere
case "$FILE_PATH" in
  *CLAUDE.md*) block "CLAUDE.md files are protected." ;;
esac

# Block: app config (hooks, system prompt, settings)
case "$FILE_PATH" in
  /app/config/*) block "Config files are read-only." ;;
esac

# Block: Claude CLI config
case "$FILE_PATH" in
  /root/.claude/*) block "Claude CLI config is protected." ;;
esac

exit 0
