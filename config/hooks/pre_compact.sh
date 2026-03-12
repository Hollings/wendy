#!/bin/bash
# PreCompact hook: writes a flag file so the next nudge prompt tells Wendy
# to restore context via check_messages after the compaction.
# Runs with cwd = channel workspace directory.
touch ./.compacted
exit 0
