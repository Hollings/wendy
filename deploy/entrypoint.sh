#!/bin/bash
# Wendy Bot entrypoint - setup hooks then run main command

# Setup Claude sync hooks (if config exists)
if [ -f /app/config/claude-sync/setup-hooks.sh ]; then
    bash /app/config/claude-sync/setup-hooks.sh
fi

# Execute the main command
exec "$@"
