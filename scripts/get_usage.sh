#!/bin/bash
# Get Claude Code usage via direct API call
# No interactive mode needed - just reads credentials and calls API

CREDS_FILE="/root/.claude/.credentials.json"

# Check if credentials file exists
if [ ! -f "$CREDS_FILE" ]; then
    echo '{"error": "No credentials file found"}'
    exit 1
fi

# Extract access token from credentials
ACCESS_TOKEN=$(cat "$CREDS_FILE" | grep -o '"accessToken":"[^"]*"' | cut -d'"' -f4)

if [ -z "$ACCESS_TOKEN" ]; then
    echo '{"error": "No access token found in credentials"}'
    exit 1
fi

# Call the usage API
RESPONSE=$(curl -s "https://api.anthropic.com/api/oauth/usage" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -H "User-Agent: claude-code/2.1.7" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "anthropic-beta: oauth-2025-04-20")

# Check if we got an error
if echo "$RESPONSE" | grep -q '"error"'; then
    echo "$RESPONSE"
    exit 1
fi

# Parse the response - extract utilization percentages and reset times
# API returns: five_hour (session), seven_day (week all), seven_day_opus (opus)
session_pct=$(echo "$RESPONSE" | grep -o '"five_hour":{[^}]*}' | grep -o '"utilization":[0-9.]*' | cut -d':' -f2 | cut -d'.' -f1)
session_resets=$(echo "$RESPONSE" | grep -o '"five_hour":{[^}]*}' | grep -o '"resets_at":"[^"]*"' | cut -d'"' -f4)

week_all_pct=$(echo "$RESPONSE" | grep -o '"seven_day":{[^}]*}' | grep -o '"utilization":[0-9.]*' | cut -d':' -f2 | cut -d'.' -f1)
week_all_resets=$(echo "$RESPONSE" | grep -o '"seven_day":{[^}]*}' | grep -o '"resets_at":"[^"]*"' | cut -d'"' -f4)

# seven_day_sonnet is the Sonnet-specific limit
week_sonnet_pct=$(echo "$RESPONSE" | grep -o '"seven_day_sonnet":{[^}]*}' | grep -o '"utilization":[0-9.]*' | cut -d':' -f2 | cut -d'.' -f1)
week_sonnet_resets=$(echo "$RESPONSE" | grep -o '"seven_day_sonnet":{[^}]*}' | grep -o '"resets_at":"[^"]*"' | cut -d'"' -f4)

# Output JSON
cat << EOF
{
  "session_percent": ${session_pct:-0},
  "session_resets": "${session_resets:-}",
  "week_all_percent": ${week_all_pct:-0},
  "week_all_resets": "${week_all_resets:-}",
  "week_sonnet_percent": ${week_sonnet_pct:-0},
  "week_sonnet_resets": "${week_sonnet_resets:-}",
  "timestamp": "$(date -Iseconds)"
}
EOF
