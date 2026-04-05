# Task System (bd) - Full Reference

You have a background task queue for delegating work to forked agents. Use this instead of Claude Code's built-in Task/subagent system (which is incompatible with your Discord interface).

## Workflow

1. **Create a task** with `bd create "detailed description"`
2. **Move on** - the orchestrator automatically forks your session and spawns a background agent
3. **Get notified** - when the task finishes, you'll see a notification in your next message check

Don't poll or check on tasks - notifications are automatic.

## How It Works (Session Forking)

When you run `bd create`, the system:
1. Captures your current session state (context, recent files, conversation)
2. Creates a task in the queue
3. The orchestrator picks it up and **forks your session**
4. The forked agent works in the background with your context

The agent is you, frozen at the moment you created the task.

## Commands

```bash
bd create "description"              # Create task (default: Opus, P2)
bd create "description" -p 1         # Higher priority (P0=highest, P4=lowest)
bd create "description" -l model:haiku  # Haiku for simple/cheap tasks
bd create "description" -p 0 -l model:opus  # Urgent, most capable
```

### Priority Levels
- **P0** Critical/urgent
- **P1** High priority
- **P2** Normal (default)
- **P3** Low priority
- **P4** Backlog

### Model Selection
Default is **Opus**. Use `-l model:haiku` for:
- Simple file edits
- Straightforward bug fixes
- Tasks with clear, narrow scope

## Writing Good Task Descriptions

Agents inherit your context up to when you create the task. They can't see what you do after.

Include:
- **Goal**: What should exist when this is done?
- **Specifics**: Key requirements, constraints, preferences
- **References**: Point to files/code you discussed

### Bad
```
bd create "make it better"
```

### Good
```
bd create "Fix the performance issue in the snake game we discussed.

Optimize the rendering loop in game.js - the 60fps target and the
segment redraw approach we talked about.

Test that it feels smooth even with 50+ segments."
```

The agent knows which snake game, where it lives, and what the issue is from your conversation context.

## Task Dependencies

Multiple tasks can run concurrently (up to 3). Use dependencies when order matters:

```bash
bd create "Set up database schema" -p 1
# Returns: Created task bd-abc123

bd create "Write API endpoints using the schema" -p 1
# Returns: Created task bd-def456

bd dep add bd-def456 bd-abc123   # def456 waits for abc123
```

### Dependency Commands
- `bd dep add <child> <parent>` - child waits for parent
- `bd dep list <task-id>` - show dependencies

## New Commands

### Quick Close with Reason
```bash
bd done <task-id> "summary of what was accomplished"
```
Shorthand for closing a task with a reason in one command (instead of `bd comment` + `bd close`).

### Notes
```bash
bd note <task-id> "quick note text"
```
Lighter than `bd comment` -- for quick status updates or breadcrumbs.

### Additional Flags
- `bd close <task-id> --claim-next` -- auto-claims the next highest priority task after closing
- `bd list --exclude-type <type>` -- filter out specific issue types
- `bd ready --exclude-type <type>` -- same for ready queue
- `bd list --status open,in_progress` -- comma-separated status filter

## When to Use Tasks

**Use tasks for:**
- Building new projects or features (games, sites, tools)
- Complex multi-file changes
- Work that takes more than a few minutes
- Things you want to hand off completely

**Don't use tasks for:**
- Quick fixes you can do yourself in under a minute
- Reading files or exploring code
- Simple questions or lookups

## Important Notes

- Agents work in `/data/wendy/coding/`
- Agents cannot deploy sites or send Discord messages - you do that after reviewing their work
- Agents use `bd comment <task_id> "notes"` to leave context about their work
- Task created = session forked. The agent won't see anything you do after creating the task
