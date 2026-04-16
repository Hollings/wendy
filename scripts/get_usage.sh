#!/bin/bash
# Get Claude Code usage via direct API call
# No interactive mode needed - just reads credentials and calls API

# Use CLAUDE_CODE_OAUTH_TOKEN env var (primary), fall back to credentials file
ACCESS_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"

if [ -z "$ACCESS_TOKEN" ]; then
    CREDS_FILE="/root/.claude/.credentials.json"
    if [ -f "$CREDS_FILE" ]; then
        ACCESS_TOKEN=$(cat "$CREDS_FILE" | grep -o '"accessToken":"[^"]*"' | cut -d'"' -f4)
    fi
fi

if [ -z "$ACCESS_TOKEN" ]; then
    echo '{"error": "No access token (set CLAUDE_CODE_OAUTH_TOKEN or create credentials file)"}'
    exit 1
fi

# Call the usage API (capture HTTP status code alongside body)
TMPFILE=$(mktemp /tmp/usage_response.XXXXXX)
HTTP_CODE=$(curl -s -o "$TMPFILE" -w '%{http_code}' \
    "https://api.anthropic.com/api/oauth/usage" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -H "User-Agent: claude-code/2.1.7" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "anthropic-beta: oauth-2025-04-20")
RESPONSE=$(cat "$TMPFILE" 2>/dev/null)
rm -f "$TMPFILE"

# Check for HTTP-level auth/rate-limit errors
if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    echo "{\"error\": \"Unauthorized ($HTTP_CODE) - token may lack required scope\"}"
    exit 1
fi
if [ "$HTTP_CODE" = "429" ]; then
    echo "{\"error\": \"Rate limited (429) - too many requests\"}"
    exit 1
fi
if [ "$HTTP_CODE" != "200" ]; then
    echo "{\"error\": \"HTTP $HTTP_CODE from usage API\"}"
    exit 1
fi

# Check if we got an error in the response body
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
