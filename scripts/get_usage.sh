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

# Parse the response - single-pass jq with null handling
OUTPUT=$(echo "$RESPONSE" | jq -e '{
  session_percent: (.five_hour.utilization // 0 | floor),
  session_resets: (.five_hour.resets_at // ""),
  week_all_percent: (.seven_day.utilization // 0 | floor),
  week_all_resets: (.seven_day.resets_at // ""),
  week_sonnet_percent: (.seven_day_sonnet.utilization // 0 | floor),
  week_sonnet_resets: (.seven_day_sonnet.resets_at // ""),
  timestamp: now | todate
}')

if [ $? -ne 0 ]; then
    echo '{"error": "Failed to parse usage response"}'
    exit 1
fi

echo "$OUTPUT"
