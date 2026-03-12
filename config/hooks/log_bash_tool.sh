#!/bin/bash
# PostToolUse hook for Bash: logs every bash tool call to SQLite.
#
# Captures the command, description, working directory, exit code,
# and output so ephemeral scripts can be found later without parsing
# session JSONL files.
#
# Runs async so it never slows down Claude's execution.

INPUT=$(cat)

DB_PATH="/data/wendy/shared/wendy.db"

# Bail if DB doesn't exist yet (first startup)
if [ ! -f "$DB_PATH" ]; then
  exit 0
fi

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
DESCRIPTION=$(echo "$INPUT" | jq -r '.tool_input.description // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

# Extract exit code and output from tool_response
# tool_response can be a string or object depending on outcome
EXIT_CODE=$(echo "$INPUT" | jq -r '.tool_response.exitCode // .tool_response.exit_code // empty')
# Truncate output to 10000 chars to avoid bloating the DB
OUTPUT=$(echo "$INPUT" | jq -r '
  .tool_response.stdout // .tool_response.output // .tool_response // empty
  | if type == "string" then . else tostring end
  | .[0:10000]
')

if [ -z "$COMMAND" ]; then
  exit 0
fi

# Ensure the table exists (idempotent)
sqlite3 "$DB_PATH" "CREATE TABLE IF NOT EXISTS bash_tool_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  command TEXT NOT NULL,
  description TEXT,
  cwd TEXT,
  exit_code INTEGER,
  output TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);" 2>/dev/null

sqlite3 "$DB_PATH" "INSERT INTO bash_tool_log (session_id, command, description, cwd, exit_code, output)
VALUES (?, ?, ?, ?, ?, ?);" \
  "$SESSION_ID" "$COMMAND" "$DESCRIPTION" "$CWD" "$EXIT_CODE" "$OUTPUT" 2>/dev/null

exit 0
