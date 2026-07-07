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

# Insert via python3: the sqlite3 CLI does NOT bind extra args to ?
# placeholders (it executes them as additional SQL), so the previous
# sqlite3-based insert silently failed on every call.
python3 - "$DB_PATH" "$SESSION_ID" "$COMMAND" "$DESCRIPTION" "$CWD" "$EXIT_CODE" "$OUTPUT" <<'PY' || true
import sqlite3
import sys

db, sid, cmd, desc, cwd, code, out = sys.argv[1:8]
conn = sqlite3.connect(db, timeout=10)
try:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bash_tool_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            command TEXT NOT NULL,
            description TEXT,
            cwd TEXT,
            exit_code INTEGER,
            output TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO bash_tool_log (session_id, command, description, cwd, exit_code, output)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (sid or None, cmd, desc or None, cwd or None,
         int(code) if code.lstrip("-").isdigit() else None, out or None),
    )
    conn.commit()
finally:
    conn.close()
PY

exit 0
