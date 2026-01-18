# Task System (bd)

You have a task queue system for delegating work to background agents. Use this instead of Claude Code's built-in Task/subagent system.

## Workflow

1. **Create a task** with `bd create "detailed description"`
2. **Move on** - the orchestrator automatically forks your session and spawns a background agent to work on it. Respond to the user, do other things, or just wait.
3. **Get notified** - when the task finishes, you'll see it in your next message check

That's it. Don't poll or check on tasks - you'll be notified automatically.

## How It Works (Session Forking)

When you run `bd create`, the system:
1. Captures your current session state (your context, knowledge, recent work)
2. Creates a task in the queue
3. Orchestrator picks it up and **FORKS your session**
4. The forked agent works in the background with YOUR context

This means agents already know:
- What project you're working on
- Files you've been editing
- What the user asked for
- Context from your conversation

The agent is essentially "you, frozen in time" with limited capabilities and a specific task.

## Creating Good Tasks

Since agents inherit your context, you can be **more concise** than before. But remember:
- The agent sees your conversation UP TO when you created the task
- It can't see what you do AFTER creating the task
- It still needs a clear goal

Include:

- **Goal**: What should exist when this is done?
- **Specifics**: Key requirements, constraints, or preferences
- **References**: Point to files/code you discussed (agent has that context)

### Bad task (too vague)
```
bd create "make it better"
```

### Old way (before forking - overly verbose)
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

### New way (with forking - agent already has context)
```
bd create "Fix the performance issue in the snake game we discussed.

Optimize the rendering loop in game.js - the 60fps target and the
segment redraw approach we talked about.

Test that it feels smooth even with 50+ segments."
```

The agent knows:
- Which snake game (from your conversation)
- Where it's located (you looked at the files)
- What the performance issue is (you discussed it)
- The vanilla JS + canvas approach (from the code it saw)

### When to Add More Detail

Still include specifics when:
- Making a decision the agent wouldn't know ("use React, not Vue")
- Referencing something not in recent context
- Overriding something from the conversation
- Working on a project you haven't discussed recently

If in doubt, add more context - but leverage what the agent already knows.

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

- **Agents fork from YOUR session** - they have your context up to the moment you create the task
- Default model is Haiku (use `-l model:opus` for complex work)
- Agents work in `/data/wendy/coding/wendys_folder/`
- Agents CANNOT deploy or send Discord messages - you do that after reviewing
- One task runs at a time (queued if busy)
- Agents use `bd comment <task_id> "notes"` to leave context about their work
- **Task created = session forked** - the agent won't see what you do AFTER creating the task
