# Async message queue — design review & refined design

> Status: implemented (see "Implementation" below). Supersedes the greenfield
> proposal in bead `wv-e4m`. Phase-1 review by polecat `furiosa` (hq-d5f).

## 1. What the original proposal (wv-e4m) assumed

`wv-e4m` proposed replacing Wendy's "poll-based and ad-hoc" inbound handling with a
Gas-Town-mail-inspired durable queue: stable IDs, read/unread, threading, priority,
and harness-injected turn-boundary notifications that read **queue state** instead of
grepping the CLI transcript. It named three pain points:

1. the watermark / poll model,
2. a `msg_reminder.sh` Stop hook that greps the transcript for `msg` usage, and
3. `msg --force`'s mid-turn interrupt check being the only concurrency guard.

## 2. Critical review — what is actually true in this codebase

**The premise is partly stale, and the store is already a durable queue.**

- **`msg_reminder.sh` does not exist** on this branch. The live Stop hooks
  (`journal_stop_check.sh`, `prompt_bookkeeping.sh`) are counter/time-driven and do
  **not** parse the transcript. There is no transcript-grep to remove. The correct
  posture is therefore: *do not (re)introduce transcript parsing*, and add the missing
  state-driven reminder instead.

- **Inbound messages are already a durable, ID-addressed, de-duplicated queue.**
  `message_history` is SQLite (WAL), keyed by the stable Discord snowflake
  `message_id`, with `INSERT OR IGNORE` providing idempotent dedup, and it survives
  restarts. `channel_last_seen` is a durable per-channel read cursor. A greenfield
  schema would add migration risk to a live bot for little benefit. **Refine, don't
  rewrite.**

### Concrete bugs / risks found

| # | Sev | Problem |
|---|-----|---------|
| 1 | HIGH | **Mid-turn crash loses data.** `handle_check_messages` advances the watermark **and deletes** consumed synthetic messages at *read* time, before Wendy acts. If the CLI dies after the read but before she replies (timeout / kill / cancel / crash), synthetic notifications (task-done "YOU MUST announce", self-wakes, webhooks, interrupt/context notices) are gone forever, and real messages fall behind the watermark so `has_pending_messages` returns False — no auto-restart, the user's message goes unanswered. The orchestrator only restored the watermark on the `overloaded` path; every other failure path leaked. |
| 2 | MED | **`msgs --peek` is a silent no-op.** `bin/msgs` sends `?peek=true`, but `handle_check_messages` never read it, so the watermark advanced anyway. A documented "read without advancing" capability did nothing. |
| 3 | MED | **Notifications were surfaced twice.** A `task_completion` was consumed both by `watch_notifications` (→ synthetic message, `seen_by_wendy`) and by `check_messages` (→ `task_updates`, `seen_by_proxy`, rendered separately by `bin/msgs`). Two representations of one event; the two seen-flags can drift. |
| 4 | MED | **No state-driven turn-boundary reminder.** The only "go read" signal was the unconditional stdin nudge at generation start; nothing caught a turn that *ended* with unread messages. |
| 5 | LOW | Watermark is a single scalar — no per-message read/unread, threading or priority. Adequate for a linear channel; threading/priority is YAGNI here. |
| 6 | LOW | The send-interrupt (`check_for_new_messages`) ignores synthetics, so a send can race ahead of a freshly-arrived system notification. |

## 3. Refined design (what we build)

Keep the existing durable store. Add **ack-on-turn-completion** so delivery is
crash-safe, fix the peek/dedup bugs, and add the missing state-driven hook.

### 3.1 Two cursors instead of destructive reads

The watermark (`channel_last_seen.last_message_id`) keeps its existing role as the
**seen** cursor — it advances when Wendy reads, and the send-interrupt compares
against it to detect *newer* mid-turn arrivals (unchanged behaviour, which the
interrupt feature depends on).

Consumption is now **committed at the turn boundary**, not at read time:

- **Read** (`check_messages`, non-`peek`): advance the seen cursor as before, but
  **mark** consumed synthetics `delivered = 1` instead of deleting them.
- **Turn success** (orchestrator, after `run_cli` returns): `commit_turn` deletes the
  delivered synthetics. The seen cursor stays put — it is now the committed position.
- **Turn failure** (any exception — timeout, cancel, overloaded, crash):
  `rollback_turn` resets the seen cursor back to where it was at the *start* of the
  turn and un-marks (`delivered = 0`) this channel's delivered synthetics, so the next
  turn re-reads everything. This is **at-least-once delivery**.

This is the minimal change that closes bug #1: one new column
(`message_history.delivered`), three small state methods, and a generalisation of the
orchestrator's existing `saved_last_seen` restore from "overloaded only" to "all
failures, plus synthetics".

### 3.2 `peek` honoured server-side

`check_messages?peek=true` returns messages **without** advancing the seen cursor or
marking synthetics delivered (fixes bug #2).

### 3.3 One notification path

`check_messages` no longer consumes notifications as `task_updates`; task completions
surface exactly once, as synthetic messages via `watch_notifications` (fixes bug #3).
`bin/msgs` drops its `--- task updates ---` rendering. The `seen_by_proxy` column is
retained for compatibility but no longer drives a second surfacing path.

### 3.4 State-driven Stop hook

`unread_messages_stop_check.sh` queries SQLite at Stop time for unread, non-bot real
messages in the channel (`message_id > seen cursor`). If any exist it blocks **once**
(guarded by `stop_hook_active`) telling Wendy to run `msgs` and respond. It reads
**queue state**, never the transcript (fixes bug #4). It is a no-op when the DB or
`WENDY_CHANNEL_ID` is unavailable, so it can never wedge a turn.

### 3.5 Explicitly deferred

Per-message read/unread, threading and priority (bug #5) are **not** built: a linear
per-channel cursor is sufficient for Discord's model, and the durability fix above is
what actually mattered. Tracked as a follow-up bead.

## 4. Implementation

| Area | Change |
|------|--------|
| `wendy/state.py` | `message_history.delivered` column + migration; `fetch_messages` excludes delivered synthetics; `mark_synthetics_delivered`, `commit_delivered_synthetics`, `rollback_turn`, `count_unread_real_messages`. |
| `wendy/api_server.py` | `handle_check_messages` marks (not deletes) synthetics, honours `peek`, drops `task_updates`. |
| `wendy/discord_client.py` | `_generate_response` commits on success / rolls back on every failure (replaces overloaded-only watermark restore). |
| `bin/msgs` | drop `task_updates` rendering. |
| `config/hooks/unread_messages_stop_check.sh` + `config/claude_settings.json` | state-driven Stop reminder. |
| `tests/test_state.py` | delivery / commit / rollback / unread-count coverage. |

### Delivery state machine (per synthetic message)

```
inserted (delivered=0)
   │  check_messages (non-peek) returns it
   ▼
delivered=1  ──turn success──▶ deleted
   │
   └─────────turn failure────▶ delivered=0 (re-read next turn)
```
