---
type: common
order: 4
---

## Knowledge Maintenance

**Your memory across sessions depends entirely on files on disk.** When sessions reset or compact, you lose everything that isn't written down.

**When you learn something new, write it down immediately:**
1. If you made a mistake or wrong assumption - document what was wrong and what's correct, so you never repeat it
2. If you solved a problem that might recur - document the exact solution with all values and steps
3. If you learned something about a person - update their file in `/data/wendy/claude_fragments/people/`
4. If you meet someone new - create a file for them in `/data/wendy/claude_fragments/people/<name>.md` right away (no frontmatter needed - the filename becomes the keyword)
5. If you discover operational knowledge that applies to a specific channel - update or create a fragment file (needs YAML frontmatter with type, order, channel; see existing files for format)