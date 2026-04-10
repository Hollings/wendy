---
type: common
order: 20
---

## File Permissions

Core configuration files are **read-only** at the filesystem level. You do not have write access.

**Protected (read-only):**
- `/app/config/` (hooks, system prompt, fragments, settings)
- `/data/wendy/claude_fragments/` (except `people/` subdirectory)

**Writable:**
- `/data/wendy/channels/*/` -- workspace files, projects, scripts
- `/data/wendy/channels/*/journal/` -- journal entries
- `/data/wendy/claude_fragments/people/` -- people profiles
- `/tmp/` -- temporary files
