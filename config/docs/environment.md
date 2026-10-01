# Your conversation environment

You control message delivery for this conversation.

```bash
wenv status
wenv messages manual
wenv messages auto
wenv client warm
wenv client cold
```

Manual is the default. New messages wake you with a content-free notice, but their
contents are delivered only when you call `msgs` (or its check_messages API).
You may ignore a notice and finish. Sending never fetches messages or marks them
read. If a send is blocked by an unread notice, read with `msgs` or use `msg --force`
to send deliberately without reading. No polling is necessary.

Auto is opt-in. Messages arrive as labeled observations before a turn or after a
tool finishes. They do not interrupt a thought or a running tool. If messages
arrive just before you finish, a Stop hook delivers one final batch. `msgs` remains
available for explicit reads. Switching to manual stops future automatic delivery;
it cannot remove observations you have already received.

Preferences belong to this conversation, so threads have independent settings.
Background workers cannot change them. Lunch/enrichment suppresses all automatic
delivery. Read cursors and task notifications retain turn commit/rollback behavior.

Warm keeps the Claude process idle between turns, with no repeated model calls
while waiting. The host retains up to four idle clients for ten minutes by default.
Cold releases the process after this turn. Neither option deletes saved sessions,
workspaces, or checkpoints. Model, session, permission, or system-prompt changes can
require a fresh process; disk history still resumes. Warm transport is an optimization,
not a guarantee of a fixed response time.

Operator controls: `WENDY_PERSISTENT_CLIENTS=0` restores one-shot transport;
`WENDY_CLIENT_IDLE_SECONDS` and `WENDY_WARM_CLIENT_LIMIT` bound idle retention.
