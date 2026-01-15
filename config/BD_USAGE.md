# Task System (bd)

You have a task queue system for delegating work to background agents. Use this instead of Claude Code's built-in Task/subagent system.

## Workflow

1. **Create a task** with `bd create "detailed description"`
2. **Move on** - the orchestrator automatically spawns a Claude agent to work on it in the background. Respond to the user, do other things, or just wait.
3. **Get notified** - when the task finishes, you'll see it in your next message check

That's it. Don't poll or check on tasks - you'll be notified automatically.

## Creating Good Tasks

**CRITICAL:** Task agents have ZERO context from your conversation. They don't know:
- What the user asked for
- What you discussed
- What files exist
- What you already tried

Every task description must be **completely self-contained**. Include:

- **Goal**: What should exist when this is done?
- **Context**: Why is this being built? What's it for?
- **Requirements**: Specific features, behaviors, constraints
- **Location**: Where should files go? What's the project name?
- **Style/approach**: Any preferences for how it should be built?

### Bad task (too vague)
```
bd create "make the snake game faster"
```

### Good task (self-contained)
```
bd create "Improve snake game performance in /data/wendy/wendys_folder/snake-game/

Current issue: The game feels sluggish, especially when the snake gets long.

Goals:
- Smooth 60fps gameplay
- No lag when snake has 50+ segments
- Keep the same visual style

The game uses vanilla JS with canvas. Look at public/game.js for the main loop.
Consider: requestAnimationFrame timing, efficient collision detection,
maybe only redraw changed segments instead of full redraws.

Test by playing the game and checking that movement feels responsive."
```

## Commands

```bash
bd create "description"                        # Create a task (default priority P2)
bd create "description" -p 1                   # Higher priority (P0=highest, P4=lowest)
bd create "description" -l model:opus          # Use Opus instead of Haiku
bd create "description" -p 0 -l model:opus     # Urgent + Opus
```

### Priority Levels
- **P0**: Critical/urgent - do this first
- **P1**: High priority
- **P2**: Normal (default)
- **P3**: Low priority
- **P4**: Backlog/whenever

### Model Selection
By default, tasks run with **Haiku** (fast and cheap). Add `-l model:opus` for complex work that needs more capability. Use Opus for:
- Architectural decisions
- Complex debugging
- Work that failed with Haiku

You rarely need `bd list` or `bd show` - just create tasks and wait for notifications.

## When to Use Tasks

**Use tasks for:**
- Building new projects/features (games, sites, tools)
- Complex multi-file changes
- Work that takes more than a few minutes
- Things you want to hand off completely

**Don't use tasks for:**
- Quick fixes (just do them yourself)
- Reading files or exploring code
- Simple questions or lookups
- Anything you can do in under a minute

## Important Notes

- Default model is Haiku (use `-l model:opus` for complex work)
- Agents can only work in `/data/wendy/wendys_folder/`
- Agents CANNOT deploy - you deploy after reviewing their work
- One task runs at a time (queued if busy)
- **Task descriptions are read immediately** - updating a task after creation won't help. If you need to change the description, close the task and create a new one.
