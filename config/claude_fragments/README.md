# Fragment Files

Fragment files are markdown documents with YAML frontmatter that get assembled into Wendy's system prompt. Each file is self-contained -- its frontmatter declares when and where it should load.

## Frontmatter Schema

Every `.md` file in this directory needs a `---` frontmatter block at the top:

```markdown
---
type: topic
order: 1
keywords: [osrs, runescape, bond]
---
# Your content here...
```

### Fields

| Field | Required | Type | Description |
|---|---|---|---|
| `type` | yes | `common\|channel\|person\|topic\|anchor` | Determines loading behavior and prompt position |
| `order` | no | int (default 50) | Sort order within type group (lower = earlier) |
| `channel` | for `channel` type | str | Discord channel ID this fragment belongs to |
| `keywords` | no | list[str] | Keyword strings to match against recent messages |
| `match_authors` | no | bool (default false) | Also check keywords against message author names |
| `select` | no | str (multiline) | Custom Python selection logic (overrides keywords) |

### Type Behavior

| Type | When loaded | Prompt position |
|---|---|---|
| `common` | Always, all channels | After base system prompt |
| `channel` | Always, when `channel` matches current channel ID | After common fragments |
| `person` | When keywords/select match (always if no rules) | Top of prompt (before channel) |
| `topic` | When keywords/select match recent messages | After thread context |
| `anchor` | Always, all channels | Very bottom of prompt |

## Examples

### Common fragment (always loaded)
```markdown
---
type: common
order: 1
---
## Communication Style
Be casual and friendly...
```

### Channel-specific fragment
```markdown
---
type: channel
order: 5
channel: "1461429474250850365"
---
## Deployment Notes
This channel is for coding...
```

### Person profile (keyword + author matching)
```markdown
---
type: person
order: 1
keywords: [alice, alicecodes]
match_authors: true
---
# Alice
She likes Python and cats...
```

### Topic with keywords
```markdown
---
type: topic
order: 3
keywords: [docker, container, kubernetes, k8s]
---
# Docker Reference
...
```

### Topic with custom select logic
```markdown
---
type: topic
order: 6
select: |
  webhook_bots = {"wendy's inbox", "ge floor goblin"}
  return any(a in webhook_bots for a in authors)
---
# Webhook Engagement
Respond to webhook messages...
```

## Custom `select` Snippets

The `select` field lets you write Python code that decides whether the fragment loads. The code is wrapped in a function and executed safely.

### Available Variables

| Variable | Type | Description |
|---|---|---|
| `messages` | list[dict] | Last 8 messages: `{"author": str, "content": str}` |
| `authors` | list[str] | Author names (lowercased) |
| `channel_id` | str | Current Discord channel ID |
| `combined` | str | All message content joined and lowercased |

### Available Builtins

`any`, `all`, `len`, `str`, `int`, `bool`, `list`, `set`, `min`, `max`, `sorted`, `enumerate`, `zip`, `range`, `isinstance`, `True`, `False`, `None`

### Rules
- Must `return` a truthy/falsy value
- Max 2000 characters
- If it throws an exception, the fragment is skipped (not loaded)
- No imports, no file I/O, no network access

## Creating a New Topic

1. Create a file: `topic_XX_your_topic.md` (XX = order number)
2. Add frontmatter with `type: topic` and `keywords`
3. Write your content below the frontmatter
4. Deploy -- the file gets seeded to `/data/wendy/claude_fragments/` on startup

That's it. No manifest file to update, no code changes needed.

## Ordering

Within each type group, fragments are sorted by `order` (ascending). Use gaps to leave room for future insertions:
- 01-09: Core/foundational
- 10-29: Standard content
- 30-49: Supplementary
- 50: Default (if order not specified)
- 51-99: Late additions

## File Naming Convention

While the frontmatter is the source of truth for type/order, the filename convention helps with organization:

```
{identifier}_{order}_{descriptive_title}.md
```

- `common_01_communication_style.md`
- `1461429474250850365_05_deployment.md` (channel ID prefix)
- `person_01_hollings.md`
- `topic_01_runescape.md`
- `anchor_01_behavior.md`
