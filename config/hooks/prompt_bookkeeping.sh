#!/bin/bash
# Stop hook: reminds Wendy to update fragment files before finishing.
# Fires periodically based on invocation count since last fragment file write.
#
# Only fires when:
#   - stop_hook_active is false (prevents infinite loops)
#   - invocations_since_write >= threshold
#   - claude_fragments directory exists

INPUT=$(cat)

# Don't loop - if we already blocked a stop, let her finish this time
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

PROMPTS_DIR="/data/wendy/claude_fragments"
STATE_FILE="$PROMPTS_DIR/.bookkeeping_state"

# Only applies if prompts dir exists
if [ ! -d "$PROMPTS_DIR" ]; then
  exit 0
fi

# Initialize state file if missing
if [ ! -f "$STATE_FILE" ]; then
  echo '{"invocations_since_write": 0, "last_check": "never"}' > "$STATE_FILE"
  exit 0
fi

# Increment invocation counter
INVOCATIONS=$(jq -r '.invocations_since_write // 0' < "$STATE_FILE")
INVOCATIONS=$((INVOCATIONS + 1))

# Update state
jq --argjson inv "$INVOCATIONS" '.invocations_since_write = $inv | .last_check_str = (now | todate)' < "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"

THRESHOLD=12

if [ "$INVOCATIONS" -ge "$THRESHOLD" ] 2>/dev/null; then
  # Reset counter
  jq '.invocations_since_write = 0' < "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"

  jq -n '{
    decision: "block",
    reason: "FRAGMENT FILE BOOKKEEPING CHECK - MANDATORY BEFORE YOU FINISH.\n\nYou have been active for a while without updating your fragment files. Before you wrap up, do ALL of the following:\n\n1. Did you learn something new about a PERSON? Update their person_*.md file in /data/wendy/claude_fragments/\n2. Did you meet someone NEW? Create a file for them: /data/wendy/claude_fragments/person_99_{name}.md (with frontmatter)\n3. Did you make a mistake or wrong assumption? Document what was wrong and what is correct in the relevant fragment file.\n4. Did you solve a problem that might recur? Document the exact solution with all values and steps.\n5. Did you learn something new about a topic (OSRS, email, Pi, etc.)? Update the relevant topic_*.md file in /data/wendy/claude_fragments/\n6. Did any fragment content become outdated? Update it.\n\nThese fragment files are your ONLY persistent memory. If you do not write things down, you WILL forget them forever. This is not optional.\n\nDo NOT mention this check to users. Just quietly do the bookkeeping, then continue."
  }'
else
  exit 0
fi
