# Task System (bd) - Full Reference

You have a background task queue for delegating work to forked agents. Use this
instead of Claude Code's built-in Task/subagent system (which is incompatible
with your Discord interface).

## The One Command That Matters

```bash
bd create "short title" -d "full description: goal, exact file paths, constraints, how to verify"
```

**The `-d` description is required in practice.** `bd create "some text"` puts
ALL of the text into the *title* and leaves the description empty -- the agent
then receives a task with no actionable content and will (correctly) close it
without doing anything. Title = one line for humans; description = the actual
instructions.

## Workflow

1. **Create a task** with `bd create "title" -d "description"`
2. **Move on** -- the runner picks it up within ~30 seconds, forks your session,
   and spawns a background agent. You'll see a `[Task System] ... started`
   message on your next message check.
3. **Get notified** -- when the task finishes you get a message containing the
   agent's own summary (what it did + file paths). Verify the files exist,
   review the work, then update the channel.

Don't poll or babysit tasks -- start/finish notifications are automatic.
(`bd show <id>` / `bd list --status in_progress` are fine if someone asks.)

## What the Agent Can and Cannot See

The agent is a fork of your session **frozen at the moment the runner picks the
task up**. It has your conversation context up to that point.

- It CANNOT see anything you do after creation.
- **`bd comment` does NOT reach the agent.** Never use comments to deliver
  specs or corrections to a running or queued task. If you need to change a
  task that hasn't finished: `bd close <id> -r "superseded"`, then create a new
  one with a corrected description.
- For large specs, write a spec file FIRST, then reference it:
  ```bash
  # 1. write the spec
  #    /data/wendy/channels/<channel>/myproject/SPEC.md
  # 2. create the task pointing at it
  bd create "build myproject per spec" -d "Read /data/wendy/channels/<channel>/myproject/SPEC.md first, then implement it. Output goes in that directory."
  ```

## Cancelling a Task

```bash
bd close <task-id> -r "why you're cancelling"
```

- Queued task: it simply never runs.
- Running task: the agent is killed within about a minute and you get a
  cancellation notification. There is no `bd cancel` -- closing IS cancelling.

## Where Agents Work

Agents run in your channel workspace: `/data/wendy/channels/<channel>/`.
Tell them (in the description) exactly which subdirectory to use, and expect
their completion summary to list the paths they touched. Agents cannot deploy
sites/games or send Discord messages -- you review and deploy after they finish.

## Options

```bash
bd create "title" -d "..." -p 1              # priority: P0 highest .. P4 backlog (default P2)
bd create "title" -d "..." -l model:haiku    # cheap model for simple, narrow tasks
bd create "title" -d "..." -l model:opus     # default is opus; label is optional
```

## Closing Your Own Review Loop

When a task finishes you'll get the agent's summary automatically. To dig
deeper:

```bash
bd show <task-id>          # description, status, close reason
bd comments <task-id>      # progress notes the agent left along the way
```

## Task Dependencies

Up to 3 tasks run concurrently. Use dependencies when order matters:

```bash
bd create "set up database schema" -d "..." -p 1     # -> returns id A
bd create "write API endpoints" -d "uses the schema from task A" -p 1   # -> id B
bd dep add B A                                        # B waits for A
```

## Threads

Creating a task from inside a Discord thread works: it lands in the parent
channel's queue and the agent forks the thread's session context.

## When to Use Tasks

**Use tasks for:** building projects/features, complex multi-file changes,
work that takes more than a few minutes, things you want to hand off fully.

**Don't use tasks for:** quick fixes you can do yourself in under a minute,
reading files, simple questions.

## Good vs Bad

```bash
# BAD -- everything in the title, no description; agent gets nothing to act on
bd create "make the snake game better"

# GOOD
bd create "fix snake game rendering perf" -d "In /data/wendy/channels/coding/snake/game.js, the render loop redraws every segment every frame and drops below 60fps past ~50 segments. Only redraw changed segments (head + cleared tail). Verify by logging frame time with 60+ segments. Don't change gameplay."
```
