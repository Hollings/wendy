#!/bin/bash
# PostToolUse hook for Read tool: reminds Wendy to also call analyze_file
# when she reads an image file.
#
# Claude Code feeds hook JSON on stdin with tool_input.file_path.
# We output JSON with additionalContext so Claude gets the reminder.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# Check if the file has an image extension
case "${FILE_PATH,,}" in
  *.png|*.jpg|*.jpeg|*.webp|*.heic|*.heif|*.gif|*.bmp)
    jq -n --arg path "$FILE_PATH" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        additionalContext: ("IMPORTANT: You just viewed an image with Read, but you MUST ALSO call the analyze_file endpoint for better accuracy. Run: curl -X POST http://localhost:8945/api/analyze_file -F \"file=@" + $path + "\" -F \"prompt=Describe this file in full detail, 5-10 sentences. Include all visible text, objects, people, colors, and context.\" -- Trust the analyze_file result MORE than your own Read for identifying details, text, faces, and objects. Do NOT skip this step.")
      }
    }'
    ;;
  *)
    exit 0
    ;;
esac
