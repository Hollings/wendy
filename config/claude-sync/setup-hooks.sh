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

echo "[claude-sync] Hooks configured"
