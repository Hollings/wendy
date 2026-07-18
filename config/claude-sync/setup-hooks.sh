#!/bin/bash
# Setup Claude Code sync hooks
# Called from entrypoint before main command

HOOKS_DIR="/root/.claude/hooks"
SETTINGS_FILE="/root/.claude/settings.json"
SOURCE_DIR="/app/config/claude-sync"

# Create hooks directory
mkdir -p "$HOOKS_DIR"

# Copy sync script if not exists or update if source is newer
if [ -f "$SOURCE_DIR/sync-session.sh" ]; then
    cp "$SOURCE_DIR/sync-session.sh" "$HOOKS_DIR/sync-session.sh"
    chmod +x "$HOOKS_DIR/sync-session.sh"
fi

# Merge settings.json (preserve existing settings, add hooks if missing)
if [ -f "$SOURCE_DIR/settings.json" ]; then
    if [ -f "$SETTINGS_FILE" ]; then
        # Merge: keep existing settings, add hooks from source
        # Use jq to merge if available, otherwise just check if hooks exist
        if command -v jq &> /dev/null; then
            # Check if hooks already configured
            if ! jq -e '.hooks.SessionEnd' "$SETTINGS_FILE" &>/dev/null; then
                # Merge settings
                jq -s '.[0] * .[1]' "$SETTINGS_FILE" "$SOURCE_DIR/settings.json" > /tmp/settings_merged.json
                mv /tmp/settings_merged.json "$SETTINGS_FILE"
            fi
        fi
    else
        # No existing settings, just copy
        cp "$SOURCE_DIR/settings.json" "$SETTINGS_FILE"
    fi
fi

# Prune hook entries whose script no longer exists. Stale personal hooks left
# in the settings volume (e.g. old context-loader/prompt-bookkeeping entries)
# otherwise surface a "hook error occurred" on EVERY session -- main and beads
# agents alike.
if [ -f "$SETTINGS_FILE" ] && command -v python3 &> /dev/null; then
    python3 - "$SETTINGS_FILE" <<'PYEOF'
import json, os, sys

path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    sys.exit(0)

hooks = data.get("hooks")
if not isinstance(hooks, dict):
    sys.exit(0)

changed = False
for event in list(hooks):
    entries = hooks.get(event) or []
    for entry in entries:
        kept = []
        for h in entry.get("hooks") or []:
            cmd = (h.get("command") or "").split(" ")[0]
            if cmd.startswith("/") and not os.path.exists(cmd):
                print(f"[claude-sync] pruning stale hook: {cmd} ({event})")
                changed = True
                continue
            kept.append(h)
        entry["hooks"] = kept
    entries = [e for e in entries if e.get("hooks")]
    if entries:
        hooks[event] = entries
    else:
        del hooks[event]
        changed = True

if changed:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
PYEOF
fi

echo "[claude-sync] Hooks configured"
