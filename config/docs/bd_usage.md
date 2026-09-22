# Background tasks: wtask + BD

Use `wtask` for all task commands. The public `bd` command is deprecated: it prints
a migration warning and exits without executing anything. Do not bypass it with
another executable or direct database edits. BD remains the controller's internal
issue/dependency storage, so existing task data is retained.
Workers operate in the existing shared channel workspace. No worktrees are
created, no checkout is reset, and interrupted work is never automatically deleted.

## Quick start

```bash
wtask models
wtask start "fix rendering" -d "In /data/wendy/channels/coding/game/, fix the slow render loop. Preserve gameplay. Verify frame times with 60 segments." --model opus
wtask list
wtask show TASK_ID
wtask result TASK_ID
```

The title and full description are required. Include the goal, exact paths,
constraints and verification. For a large spec, save it first and reference its
absolute path. A worker gets that brief and an immutable excerpt of the originating
conversation; it does not fork whichever session happened to run most recently.
The originating thread/channel receives questions and results automatically.
Start notices and checkpoints are quiet; questions, failures, quota waits and
results wake Wendy. Inspect `show` for activity, checkpoints and delivery state.

## Models and global daily limits

```bash
wtask start "hard task" -d "..." --model fable
wtask model TASK_ID opus
wtask models
```

`fable` resolves to `claude-fable-5-1` and `opus` to `claude-opus-5-5` (Opus 5.5,
released 2026-09-22). Both are pinned explicit identifiers; there is no assumed
`fable-latest` or `opus-latest` alias. `sonnet` and `haiku` pass the bare CLI alias
through, so they run the newest release of that family (Sonnet 5, Haiku 4.5 as of
2026-09-08).
Model aliases come from config.py. Full IDs within supported Claude families
are also accepted. The conversational WENDY_MODEL_OVERRIDE does not override
worker choices. The default worker model is Opus.

Default policy: **10 Fable attempts/day globally**, shared by every channel,
thread and Fable version/alias. Resets at midnight America/Los_Angeles, including
DST. This limits background attempts, not Wendy's normal conversations, tokens,
turns or total account usage.

- Queueing work consumes no slot. The scheduler reserves atomically before launch.
- A definite failed subprocess launch releases a new reservation.
- A launched attempt counts even if it fails or is cancelled.
- An uncertain crash during launch conservatively retains the charge.
- Resume continues the same attempt/model/session and retains its original slot.
- Retry creates a new attempt and consumes another slot when it starts.
- Changing models on stopped/queued work starts a new attempt if the model changes.
- Exhausted work stays queued. Choose another model explicitly or wait for reset;
  the controller never silently substitutes models.

Server configuration (environment, never an agent command):

```text
WENDY_TASK_DEFAULT_MODEL=opus
WENDY_TASK_MODEL_LIMITS={"fable":10}
WENDY_TASK_QUOTA_TIMEZONE=America/Los_Angeles
```

Add other model families to the JSON to cap them. Omitted families are unlimited;
0 disables new attempts for a family. Invalid quota policy prevents worker startup.
All reservations and consumption persist in SQLite over bot restarts.

## Corrections and questions

```bash
wtask tell TASK_ID "Use the updated spec at /absolute/path/SPEC.md"
wtask show TASK_ID
```

`tell` confirms queued delivery. At the worker's next tool boundary or `inbox`
check it becomes delivered. Only `wtask ack MESSAGE_ID` by the worker marks it
acknowledged. Delivery alone does not mean the worker acted on the correction.
Long-running tools may delay delivery until they finish. Inspect `show` when
acknowledgment matters. A worker cannot submit a result while corrections remain
unacknowledged. Corrections after result submission are rejected; review the result
and retry explicitly for further work.

When a worker asks a question it saves a `needs_input` result and exits. Answer
with `tell`, then `resume`. Use `tell` for corrections, not direct BD comments.

## Stop, recover, retry

```bash
wtask stop TASK_ID "pause for a requirement change"
wtask show TASK_ID
wtask resume TASK_ID
# Or, after inspecting existing files, start a fresh attempt:
wtask retry TASK_ID
```

Stop requests terminate the worker process group. Wait for stopped feedback before
resuming or switching model. Existing files, logs, sessions, reports and checkpoints
remain. Retries receive the previous checkpoint/report and must inspect existing
work before editing. No worker logs or unfinished outputs are automatically pruned.

A bot restart holds interrupted work for explicit review/resume; it does not blindly
rerun tasks. Completed work stays completed. Legacy in-progress BD tasks are held
for review on first startup. If a saved Claude session is unavailable, resume fails
visibly; inspect the checkpoint and retry explicitly rather than silently losing context.

Only one worker runs per channel workspace, even if tasks name different projects.
Other channels can run concurrently up to ORCHESTRATOR_CONCURRENCY (default 3).
Wendy should use tell/stop before editing files that a worker is modifying.

## Dependencies

```bash
wtask start "schema" -d "..." --model opus
wtask start "API" -d "Use the schema output from TASK_A; verify ..." --after TASK_A
```

Repeat `--after` for multiple prerequisites. Dependencies are attached during BD
creation, before the issue becomes runnable. Successful worker results close BD
issues. Failed, cancelled, interrupted and needs-input tasks stay blocked in BD,
so they do not release downstream work. Use `wtask show` to inspect the recorded
prerequisites. Dependencies are specified at creation; wtask does not currently
support changing an existing task's dependency edges.

## Uncertain command response

`wtask start` prints a submission UUID before sending. If a connection fails, rerun
with the same title/description/model and `--request-id UUID`. The controller checks
both SQLite and BD for that submission and returns the existing task. The origin
metadata also lives on the BD issue so a crash between issue creation and SQLite
registration does not lose the return address or brief.

## Worker commands

```bash
wtask inbox
wtask ack MESSAGE_ID
wtask checkpoint "Implemented X; remaining Y; paths ...; checks ..."
wtask ask "Which behavior should I implement?"   # then exit
wtask finish "Implemented and verified X" --artifact /absolute/file --check "test passed"
wtask finish "Could not complete X" --outcome failed --remaining "reason"
```

Repeat `--artifact`, `--check`, and `--remaining` as needed. Artifact paths must
be absolute and exist. Reports and notifications are committed durably; a successful
exit without a structured report is a failure, not inferred success. Wendy must
review the artifacts and verification before announcing completion or deploying.

Workers get a capability restricted to their own current attempt. The messaging,
deployment and wake APIs require a controller capability; helpers authenticate
automatically. Direct controller curl requests need
`-H "Authorization: Bearer $WENDY_API_TOKEN"`. Never print or persist capabilities.
These API checks do not turn the shared Unix account/workspace into a security sandbox.
