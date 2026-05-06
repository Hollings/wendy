#!/bin/bash
# Stop hook: if Wendy finished a turn without calling the `msg` helper,
# gently remind her. Sending a Discord message is not required every turn,
# so the reminder asks "was that intentional?" rather than forcing a reply.
#
# How it detects `msg` usage:
#   - Parses the transcript JSONL from the stop-hook input
#   - Walks back to the most recent `user` entry (start of this turn)
#   - Checks every assistant `tool_use` since then for a Bash invocation
#     whose command begins with `msg ` or `msg"` (with optional leading
#     whitespace to catch indented/piped forms)

INPUT=$(cat)

# Avoid loops: if we already blocked once, let her finish this time.
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
  exit 0
fi

# Find the line number of the most recent genuine user-turn entry.
# NOTE: Claude Code writes tool_result entries as type:"user" too, so filtering
# by type alone picks up the last tool_result and breaks detection. Genuine
# user prompts have string content; tool_results have array content. The exact
# substring `"role":"user","content":"` appears only in string-content form,
# because array-content lines have `"content":[` at that position.
LAST_USER_LINE=$(grep -nE '"role":"user","content":"' "$TRANSCRIPT" | tail -1 | cut -d: -f1)
if [ -z "$LAST_USER_LINE" ]; then
  exit 0
fi

# From that line onward, extract every Bash tool_use command.
COMMANDS=$(tail -n +"$LAST_USER_LINE" "$TRANSCRIPT" \
  | jq -r 'select(.type == "assistant") | .message.content[]?
           | select(.type == "tool_use" and .name == "Bash")
           | .input.command // empty' 2>/dev/null)

# Match `msg` as a whole word. Excludes `msgs` (the read helper) because the
# trailing `s` prevents a word boundary. Matches `msg "hi"`, `msg 'hi'`,
# `msg hi`, and piped/chained forms.
if echo "$COMMANDS" | grep -qE '\bmsg\b'; then
  exit 0
fi

jq -n '{
  decision: "block",
  reason: "You did not send a message to Discord this turn. That is not required — sometimes a silent turn is the right call. But was it intentional? If you meant to reply and forgot, use `msg \"...\"` now. Otherwise just finish the turn and this reminder will not fire again."
}'
