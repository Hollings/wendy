# GEMINI.md

This file provides guidance to Gemini CLI when working with code in this repository, complementing the `CLAUDE.md` instructions.

## Project Overview
Wendy v2 is a Discord bot where each channel maintains a persistent Claude CLI session. It uses a single Python process to handle Discord interactions, session management, and a local API server for tool calls.

## Tech Stack
- **Language**: Python 3.10+ (using `asyncio`, `discord.py`)
- **CLI Subprocess**: `claude` CLI (Anthropics)
- **Database**: SQLite (shared between bot and web services)
- **Frontend**: React (Vite) for the "Brain UI"
- **Infrastructure**: Docker / Docker Compose

## Critical Rules
1. **Headless Mode**: The `claude` CLI runs in headless mode (`-p`). Wendy's final output is **NOT** sent to Discord via stdout.
2. **API Communication**: Wendy MUST use the internal API (`http://localhost:8945/api/send_message`) to send responses to Discord.
3. **Session Persistence**: Sessions are persistent JSONL files. Never delete these unless explicitly requested (`!clear`).
4. **No Circular Imports**: Follow the hierarchy defined in `CLAUDE.md`. `paths.py`, `models.py`, and `config.py` are safe for all modules to import.
5. **Security**: Never commit `.env` files or expose tokens. SENSITIVE_ENV_VARS are filtered in `cli.py`.

## Development Workflow
- **Tests**: `python3 -m pytest tests/ -v`
- **Linting**: `ruff check .`
- **Rebuild**: `./dev-rebuild.sh` (restarts the container and picks up source changes)
- **Manual CLI Access**: `docker exec -it wendy bash`

## Key Files & Directories
- `wendy/`: Core bot logic
  - `discord_client.py`: Gateway and message handling
  - `cli.py`: Claude CLI subprocess management
  - `api_server.py`: Internal HTTP API for Claude tools
  - `prompt.py`: Dynamic system prompt assembly
  - `tasks.py`: Beads background task runner
- `services/web/`: Web interface and site/game hosting
- `data/wendy/`: Persistent volume (channels, fragments, DB)
- `config/`: System prompt templates and hooks

## Common Tasks for Gemini
- **Bug Fixes**: Always reproduce first. Check `wendy.db` and `stream.jsonl` for execution traces.
- **Fragment Management**: When adding persona/knowledge, create a new `.md` file in `claude_fragments/`.
- **Tool Extensions**: To add new capabilities for Wendy, update `api_server.py` and the `TOOL_INSTRUCTIONS_TEMPLATE` in `cli.py` (or `prompt.py` in later phases).
- **Session Debugging**: Check `/root/.claude/projects/` inside the container for raw session JSONL logs.

## Verification Checklist
- [ ] Run `pytest` before and after changes.
- [ ] Ensure `ruff` linting passes.
- [ ] Check for circular imports when adding new modules.
- [ ] If changing the internal API, update the instructions provided to Claude in the system prompt.
