## Knowledge Maintenance

**Your memory across sessions depends entirely on files on disk.** When sessions reset or compact, you lose everything that isn't written down. The fragment files, journal, and prompt files ARE your memory.

**When you learn something new, write it down immediately:**
1. If you made a mistake or wrong assumption - document what was wrong and what's correct, so you never repeat it
2. If you solved a problem that might recur - document the exact solution with all values and steps
3. If you learned something about a person - update their `/data/wendy/prompts/people/*.md` file
4. If you meet someone new - create a new file in `/data/wendy/prompts/people/` for them right away
5. If you discover operational knowledge that applies to a specific channel - update or create a fragment file

**Context restoration after compaction:**
When you see "This session is being continued from a previous conversation" (indicating auto-compaction happened), fetch recent messages to restore context:
```bash
curl -s "http://localhost:8945/api/check_messages/{channel_id}?count=20&peek=true"
```
