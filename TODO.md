# TODO

## Features

- **Moderator Claude session** — a Claude session that reviews all changes to `claude.md` and hooks every 24 hours, flags or rolls back bad changes

- **Non-compact truncation mode** — instead of compacting, simply truncate the context to fit within the compaction threshold every message; test viability vs. current approach

- **Beads configurable ephemeral system prompts** — users can choose to either fork the current session (current behavior) or write their own custom system prompt for a beads agent

- **Hook loader from user folders** — load Claude hooks and `claude.md` fragments from per-user directories so changes are attributable by author; internally git-tracked for rollback (integrates with mod Claude session)

- **Robust webhook system** — extend synthetic messages to support structured webhook payloads:
  - Webhook content (e.g. a git commit message) displayed as a message
  - Attached instructions: a series of steps Wendy should take in response
  - `send_to_channel` flag to control whether the content is posted visibly in the channel
  - Users can POST structured webhooks rather than just raw text pings

- **More Discord actions** — expand the internal API with additional Discord actions Wendy can take; starting with username/display name updates, potentially also avatar, status, per-channel nicknames, role management, etc.

- **Model benchmarking system** — tooling to run Wendy against a set of test prompts/scenarios across different models and compare response quality, latency, and token usage; useful for evaluating model upgrades before switching
