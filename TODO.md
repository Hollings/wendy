# TODO

## Bugs

## Features

### High Priority

- **Filesystem access audit** — review what paths Wendy can read/write at runtime; she may currently have access to `/app/config/` (system prompt, hooks, settings) and potentially her own source code, which would let her modify her own behavior/prompts; should enforce read-only on `/app/` and restrict writes to `/data/wendy/channels/` and `/data/wendy/claude_fragments/` only

- **Resolve ping display names** — when Discord messages contain `@<1234>` ID mentions, also surface the resolved username to Wendy so she knows who is being pinged without having to decode the ID herself

- **Message wake filter / ignore list** — configurable list of bot/user IDs whose messages should NOT wake Wendy (no CLI invocation triggered) but still appear in `check_messages` responses; prevents bot feedback loops (e.g. McDonald's#4821); future-proof as an ignorelist for any noisy bots

- **Easier send_message UX** — Wendy frequently struggles with `curl` when sending messages (quote escaping, newlines in JSON bodies), falls back to writing a Python script; options: provide a wrapper shell script/binary she can call with simpler args, or a `jq`-based helper, or improve the tool docs in the system prompt with copy-paste-safe curl patterns

- **Usage-aware effort/rate limiting** — dynamically adjust Claude CLI `--effort` level (or apply soft rate limits) based on current weekly token usage relative to how far through the week we are; e.g. if 80% of budget is spent but only 50% of the week has passed, drop effort or throttle responses to avoid hitting the weekly cap early

### Medium Priority

- **Beads dep attachment on create** — update the beads dependency system to attach deps at creation time instead of the current create-then-parent flow; low urgency since tasks are usually used one at a time

- **Beads configurable ephemeral system prompts** — users can choose to either fork the current session (current behavior) or write their own custom system prompt for a beads agent

- **Robust webhook system** — extend synthetic messages to support structured webhook payloads:
  - Webhook content (e.g. a git commit message) displayed as a message
  - Attached instructions: a series of steps Wendy should take in response
  - `send_to_channel` flag to control whether the content is posted visibly in the channel
  - Users can POST structured webhooks rather than just raw text pings

- **More Discord actions** — expand the internal API with additional Discord actions Wendy can take; starting with username/display name updates, potentially also avatar, status, per-channel nicknames, role management, etc.

- **Non-compact truncation mode** — instead of compacting, simply truncate the context to fit within the compaction threshold every message; test viability vs. current approach - IMPORTANT: CHECK CACHE FUNCTIONALITY FOR THIS - prefix changing might make it cost more

### Low Priority

- **Hook loader from user folders** — load Claude hooks and `claude.md` fragments from per-user directories so changes are attributable by author; internally git-tracked for rollback (integrates with mod Claude session)

- **Moderator Claude session** — a Claude session that reviews all changes to `claude.md` and hooks every 24 hours, flags or rolls back bad changes

- **Model benchmarking system** — tooling to run Wendy against a set of test prompts/scenarios across different models and compare response quality, latency, and token usage; useful for evaluating model upgrades before switching

- **Enrichment time** — a scheduled 15-minute window at regular intervals where Wendy runs a no-prompt loop with no user input; she can do anything she wants (explore, write, create, reflect); implemented as a `tasks.py` scheduled job that fires a bare CLI invocation with no nudge, just free rein
