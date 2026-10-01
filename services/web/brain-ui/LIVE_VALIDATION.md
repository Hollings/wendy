# Live Wendy event validation — 2026-09-06

Read-only sample from the production `wendy-web` data volume over SSH:
1,765 stream frames and 1,435 frames from the eight most recent worker logs.
Private captures are kept in the gitignored `.venv/brain-live-review/` directory;
the regression tests contain synthetic values with matching event shapes.

## Results

| Check | Result |
| --- | --- |
| Source frames | 3,200, all parsed without exceptions |
| Distinct rendered rows after pairing/counter updates | 950 |
| Tool calls paired with results | 421 / 421 |
| Orphan results or unfinished calls in this sample | 0 |
| Unknown event types after fixes | 0 |
| Row and inspector rendering | 950 / 950 without exceptions |
| Structured `msgs` output | 47 / 47, including `--raw` JSON |
| Error rows | 39, including three permission denials |
| Quiet rate-limit records omitted | 58 ordinary `allowed` records |
| Automated checks | 17 frontend and 11 Python tests pass; Vite build and Ruff pass |

Observed event families: assistant text, thought summaries/token counters,
tool calls/results, session starts/results, rate limits, notifications,
status changes, compaction, CLI task starts/notifications/updates, background
task snapshots, and permission denials. Observed tools include Bash, Read,
Write, Edit, ToolSearch, Monitor, and WebFetch. This sample cannot establish
coverage of every tool or future CLI event type; unknown events retain JSON.

## Fixes from the real sample

- Permission denials now have explicit rows and appear in the Errors filter.
- CLI task updates and background-task snapshots have dedicated rendering.
- Original Claude envelopes survive normalization and tool pairing. The Event
  data tab retains request IDs, parent tool IDs, usage, permission decisions,
  result timing, model usage, and CLI session configuration.
- Context usage includes tokens being written to the cache.
- `msgs --raw` renders structured messages while preserving large Discord IDs.
- Compaction rows show before/after context counts instead of an empty body.
- Saved-log timestamps use recorded event timestamps when available; missing
  timestamps remain labeled as saved log entries.

## Deployed API compatibility

Before this deployment, the web container's older readers stripped `phase`,
`model`, `_channel`, and `close_reason`; its log endpoint lacked `log_id` and
`attempt_id`. One actual task was `status=open` with `phase=cancelled`,
demonstrating why the phase must be retained.

The rebuilt frontend and current API were deployed together to
https://wendy.monster on September 6, 2026 (Pacific), release
`20260906-235248`. Public HTTPS smoke checks passed for current HTML/CSS/JS,
rejected unauthenticated reads, valid authentication, seven channels, three
tasks and their saved logs/cursors, and 30 real WebSocket event frames.
The smoke client's receive queue must accommodate the initial replay when
closing after a partial sample; a 512-frame queue avoids a TLS shutdown stall.
The production image excludes private JSONL captures and development demos.
Only the web service was recreated; bot and game container IDs stayed unchanged.
The previous web image and source were retained for rollback.

The local live demo executes this checkout's read functions in memory over
SSH. It does not install code or restart production. It exercises current
readers against the real data volume and streams updates to the local UI.
The public Brain viewer now uses the same reader contract.

Browser validation uses the live demo: authentication, connection status,
real task phase/model rendering, saved worker history, original paired payloads,
and attempt identity. The launcher and access instructions are in README.md.
