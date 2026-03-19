# Contributing to Wendy

Ground rules for working on the Wendy codebase. Read this before adding features, config, or new modules.

## 1. Principles

1. **One source of truth.** Every piece of data or config lives in exactly one place. If you need it elsewhere, import or read from the source -- don't copy it.
2. **Config is data, not code.** If a value can change without changing program logic, it belongs in a config file or env var, not a Python constant. Exception: values that only make sense in the context of surrounding code (like `MAX_DISCORD_MESSAGES` in a truncation function).
3. **Seed data vs runtime data.** Files in `config/` are *seeds* -- they're copied to `/data/wendy/` at startup and may be modified at runtime. The `/app/config/` copies are read-only in the container. The `/data/wendy/` copies are the live versions.
4. **Modules talk through interfaces.** Services communicate via HTTP APIs, shared SQLite tables, and the filesystem (outbox, beads). They do not import each other's internals.

## 2. The CLAUDE.md Files

There are several files named `CLAUDE.md` or similar in this project. They serve very different purposes:

| File | Audience | Purpose | Editable at runtime? |
|------|----------|---------|---------------------|
| `/CLAUDE.md` | Developers and AI agents working on the codebase | Architecture docs, dev commands, gotchas | No (committed to git) |
| `/.claude/CLAUDE.md` | Local developer machine only | Gitignored secrets (webhook URLs, etc.) | Yes (local only) |
| `/data/wendy/channels/{name}/CLAUDE.md` | Wendy herself | Legacy per-channel self-notes (deprecated, migrating to fragments) | Yes (Wendy writes these) |
| `/data/wendy/claude_fragments/*.md` | Wendy herself | Fragment-based channel instructions loaded as system prompt context | Yes (Wendy writes these) |
| `config/agent_claude_md.txt` | Background agents (orchestrator) | Reference info appended to agent system prompts | No (committed to git) |
| `config/system_prompt.txt` | Wendy (via Claude CLI `--append-system-prompt`) | Wendy's personality and behavioral instructions | No (committed to git) |

**Key distinction:** `CLAUDE.md` at the repo root is for *developers*. `config/system_prompt.txt` is for *Wendy's personality*. Do not mix these up. Developer docs do not belong in the system prompt, and personality instructions do not belong in CLAUDE.md.

## 3. Configuration Hierarchy

Configuration lives in five tiers, ordered from most sensitive to most ephemeral:

### Tier 1: Secrets
**Location:** Server env_file directory (mounted read-only at `/secrets/`)

| File | Contents |
|------|----------|
| `bot.env` | `DISCORD_TOKEN`, `WENDY_CHANNEL_CONFIG`, deploy tokens |
| `sites.env` | wendy-sites service tokens |
| `games.env` | wendy-games service tokens |

**Rules:**
- Never put secrets in `docker-compose.yml`, committed `.env` files, or Python source
- Never log or print secret values
- Deployments never overwrite secrets

### Tier 2: Infrastructure
**Location:** `deploy/`

| File | Contents |
|------|----------|
| `docker-compose.yml` | Service definitions, ports, volumes, networks |
| `docker-compose.dev.yml` | Dev overrides |
| `Dockerfile` | Build instructions |

**Rules:**
- Ports, volume mounts, and service wiring go here
- Reference secrets via `env_file`, don't inline values
- If you add a new service, it goes in docker-compose.yml

### Tier 3: Behavioral Config
**Location:** `config/`

| File | Contents |
|------|----------|
| `system_prompt.txt` | Wendy's personality and instructions |
| `claude_settings.json` | Claude Code hooks (PreToolUse, PostToolUse, Stop) |
| `prompts/manifest.json` | Topic definitions with descriptions and keywords |
| `prompts/*.md` | Topic files (seeded to `/data/wendy/prompts/`) |
| `prompts/people/*.md` | Per-person context files (always loaded) |
| `hooks/*.sh` | Hook scripts referenced by claude_settings.json |
| `agent_claude_md.txt` | Reference info for background agents |
| `BD_USAGE.md` | Beads task system documentation |
| `claude_fragments/*.md` | CLAUDE.md fragment files (seeded to `/data/wendy/claude_fragments/`) |
| `claude_fragments.json` | Channel ID -> name mapping (reference for fragments) |

**Rules:**
- These files are mounted read-only at `/app/config/` in the container
- Topic files and manifest are *seeded* to `/data/wendy/prompts/` at startup -- the runtime copies are what Wendy actually reads and can edit
- Changes here require redeployment to take effect

### Tier 4: Code Constants
**Location:** Python modules

These are values that require code context to understand or change:

| Constant | Location | Purpose |
|----------|----------|---------|
| `MODEL_MAP` | `bot/claude_cli.py` | Shorthand-to-model-ID mapping |
| `MAX_DISCORD_MESSAGES` | `bot/claude_cli.py` | Session truncation threshold |
| `SENSITIVE_ENV_VARS` | `bot/claude_cli.py` | Vars filtered from CLI subprocess |
| `TOOL_INSTRUCTIONS_TEMPLATE` | `bot/claude_cli.py` | Tool permission template |

**Rules:**
- If a value could reasonably be changed by a non-developer (e.g., a model name update), consider moving it to Tier 3
- Document the constant's purpose with a comment if it's not obvious

### Tier 5: Runtime Data
**Location:** `/data/wendy/` (Docker volume, persists across deploys)

| Path | Contents | Written by |
|------|----------|------------|
| `shared/wendy.db` | All SQLite state | bot, proxy, orchestrator |
| `shared/outbox/` | Message queue JSON files | proxy (write), bot (read+delete) |
| `channels/{name}/` | Per-channel workspaces | Claude CLI |
| `channels/{name}/CLAUDE.md` | Legacy self-notes (deprecated, migrating to fragments) | Claude CLI |
| `channels/{name}/journal/` | Long-term memory entries | Claude CLI |
| `channels/{name}/attachments/` | Downloaded Discord files | bot |
| `channels/{name}/.beads/` | Task queue | Claude CLI, orchestrator |
| `claude_fragments/` | CLAUDE.md fragment files | seeded from config/, editable by Wendy |
| `claude_fragments.json` | Channel ID -> name mapping | seeded from config/ |
| `prompts/` | Runtime copies of topic files | seeded from config/, editable by Wendy |
| `secrets/runtime.json` | Runtime secrets | Wendy |
| `stream.jsonl` | Rolling event log | bot |
| `tmp/` | Scratch space | any |

**Rules:**
- Never write to `/app/` at runtime -- it's the read-only application image
- Runtime data survives deployments; don't depend on it being reset
- The `wendy_data` Docker volume is the single source of persistent state

## 4. Module Dependency Rules

### Import Hierarchy

```
bot/paths.py          (leaf - zero internal imports)
bot/conversation.py   (leaf - zero internal imports)
      |
      v
bot/state_manager.py  (imports: paths)
      |
      v
bot/context_loader.py  (imports: paths, state_manager)
bot/fragment_loader.py (imports: paths)
bot/claude_cli.py      (imports: paths, conversation, state_manager, context_loader, fragment_loader)
bot/message_logger.py (imports: paths, state_manager)
bot/wendy_cog.py      (imports: paths, conversation, state_manager, claude_cli, message_logger, context_loader, wendy_outbox)
bot/wendy_outbox.py   (imports: paths, state_manager)
```

**Rules:**
- `bot/paths.py` and `bot/conversation.py` are leaf modules. They must have zero imports from other `bot/` modules.
- `proxy/` may import from `bot.paths` and `bot.state_manager` only. It must not import cog modules, claude_cli, or anything that pulls in discord.py.
- `orchestrator/` may import from `bot.paths` and `bot.state_manager` only.
- `wendy-sites/` and `wendy-games/` are separate containers. They cannot import from `bot/`, `proxy/`, or `orchestrator/` at all.
- No circular imports.

### Cross-Service Communication

| Mechanism | Used by | Direction |
|-----------|---------|-----------|
| HTTP API (proxy) | Claude CLI -> proxy -> Discord | Bot spawns CLI, CLI curls proxy |
| Shared SQLite | bot, proxy, orchestrator | Read/write with WAL mode |
| Outbox directory | proxy (write) -> bot (read) | JSON files, polled every 0.5s |
| `.beads/` directory | CLI (write) -> orchestrator (read) | Task queue files |
| Notifications table | orchestrator/sites (write) -> bot (read) | SQLite table, polled every 5s |

## 5. Adding New Things

### Adding a new Discord channel

1. Add channel config to `WENDY_CHANNEL_CONFIG` in `bot.env` on the server
2. Choose `mode` (`"chat"` or `"full"`) and optionally `model` and `beads_enabled`
3. Restart the bot service -- `bot/paths.py:ensure_channel_dirs()` creates the workspace automatically

### Adding a new environment variable

1. Decide which tier it belongs to (see Section 3)
2. If it's a secret: add to the appropriate `.env` file on the server
3. If it's config: add to `docker-compose.yml` environment section
4. Document it in `CLAUDE.md` under "Environment Variables"
5. Add a sensible default in the code (`os.environ.get("VAR", "default")`)
6. If it contains sensitive data, add it to `SENSITIVE_ENV_VARS` in `claude_cli.py`

### Adding a new config file

1. Create the file in `config/`
2. If it needs to be editable at runtime, seed it to `/data/wendy/` at startup (see how `config/prompts/` works)
3. If read-only, it's available at `/app/config/` in the container
4. Document it in Section 3 of this file

### Adding a new topic for dynamic context

1. Create `config/prompts/yourtopic.md` with the content
2. Add an entry to `config/prompts/manifest.json` with `name`, `file`, `description`, and `keywords`
3. Deploy -- the file gets seeded to `/data/wendy/prompts/` on startup
4. The context loader will pick it up automatically based on keyword/semantic matching

### Adding a new proxy API endpoint

1. Add the route in `proxy/main.py`
2. If Claude CLI needs to call it, document the curl command in `config/system_prompt.txt`
3. If it writes to the outbox, follow the existing pattern in `send_message`
4. Add it to the Proxy API Endpoints table in `CLAUDE.md`

### Adding a new SQLite table

1. Add the `CREATE TABLE IF NOT EXISTS` statement in `bot/state_manager.py:_init_schema()` -- this is the **primary source of truth**
2. If the table is needed before the bot fully starts, also add it to `bot/message_logger.py:_init_db()` (with a comment noting it's a copy)
3. If proxy needs the table, add it to `proxy/main.py` init (with a comment noting it's a copy)
4. Add a comment: `# Schema source of truth: bot/state_manager.py`

## 6. Anti-Patterns

**Don't do these:**

- **Hardcode model IDs.** Use `MODEL_MAP` in `claude_cli.py`. Model IDs change with every release.
- **Construct filesystem paths manually.** Use `bot/paths.py`. Paths are non-obvious (e.g., session dirs use `-` encoding).
- **Parse `WENDY_CHANNEL_CONFIG` in new places.** The bot parses it once at startup. If you need channel config in proxy or orchestrator, get it through the state manager or pass it explicitly.
- **Put secrets in `docker-compose.yml`.** Use `env_file` references to secret files.
- **Duplicate schema without marking the source.** If you must duplicate a `CREATE TABLE` statement, add a comment pointing to `bot/state_manager.py` as the source of truth.
- **Import cog modules from proxy or orchestrator.** Cogs depend on discord.py which isn't needed in proxy/orchestrator. Import only from `bot.paths` and `bot.state_manager`.
- **Write to `/app/` at runtime.** The container filesystem is ephemeral. Use `/data/wendy/` for anything that needs to persist.
- **Confuse `CLAUDE.md` with `system_prompt.txt`.** CLAUDE.md is developer documentation. system_prompt.txt is Wendy's personality. They serve different audiences.
- **Add new hooks without updating `claude_settings.json`.** Hooks are registered in `config/claude_settings.json`, not discovered automatically.

## 7. Known Technical Debt

Existing violations of the rules above. Documented here so they don't get replicated:

| Issue | Location | Rule violated |
|-------|----------|--------------|
| `MODEL_MAP` duplicated | `bot/claude_cli.py` and `orchestrator/main.py` | One source of truth |
| `HAIKU_MODEL` hardcoded separately | `bot/context_loader.py` | Use MODEL_MAP |
| SQLite schema in 4 places | `state_manager`, `message_logger`, `proxy/main`, `wendy-sites/backend` | One source of truth |
| `WENDY_CHANNEL_CONFIG` parsed independently | bot, proxy, orchestrator | Single parse point |
| `TOOL_INSTRUCTIONS_TEMPLATE` is a 60-line string literal | `bot/claude_cli.py` | Config is data not code |

These aren't blocking anything today, but new code should not add to this list.
