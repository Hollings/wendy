# Security Review: wendy-v2

Generated 2026-04-07 via parallel agent review (4 agents, 22 source files).

Files are sorted by score descending. Score is 1–5 (1 = no attack surface, 5 = parses external input / handles auth / runs commands).

---

## Score 5 — Critical

### `wendy/tasks.py` — 5/5

Format string injection via untrusted task metadata.

**Line 307:**
```python
prompt = AGENT_PROMPT_TEMPLATE.format(task_id=task_id, title=title, description=description)
```
`title` and `description` come from the beads task queue (lines 291–296), which is populated externally. If an attacker controls task metadata, they can inject format placeholders: e.g., `description="{title} and {task_id}"` causes KeyError/DoS; more exotic payloads could leak internal context or manipulate agent behavior via prompt injection. Fix: use an f-string with pre-validated values, or replace `.format()` with a safe template approach that escapes `{}`/`}` before interpolation.

---

## Score 4 — High

### `wendy/api_server.py` — 4/5

HTTP API receiving Discord messages, attachments, deploy payloads, and file analysis requests.

- **Path traversal (line ~117):** `_validate_attachment_path()` checks for traversal but allows `/tmp/`; `.resolve()` follows symlinks, so a symlink under `/tmp/` can escape the intended tree.
- **Subprocess with user-controlled path (line ~615):** `ffprobe` invoked with temp file path; not shell-escaped (low risk since path is generated internally, but fragile).
- **No rate limiting on deploy endpoints (lines ~502–526):** Any holder of a valid deploy token can spam deployments indefinitely.
- **`author.display_name` stored unsanitized (lines ~102–114):** Discord display names go into SQLite without sanitization; safe today because queries are parameterized, but worth noting as a hygiene issue.

### `services/web/main.py` — 4/5

Web service: tarball extraction, Docker container management, WebSocket proxying, webhook handling.

- **Tarball path traversal (lines ~156–163):** `_safe_extract()` uses `startswith` on resolved path, but a path like `dest/x/../x` could pass if not fully normalized before comparison. `filter="data"` partially mitigates.
- **Container name injection (line ~260, 326):** Game name interpolated directly into Docker container name; if the Docker SDK passes this through a shell anywhere, characters like `;` could be interpreted.
- **Unauthenticated WebSocket proxy (lines ~410–443):** Any authenticated user can proxy to any game's WebSocket endpoint — no per-game auth.
- **`task_id` glob injection in log fetch (line ~635):** `task_id` comes from URL path and is used in a glob pattern without validation; `*` or `..` in `task_id` could enumerate or traverse log files.
- **CORS wildcard subdomain (line ~72):** Pattern `.*\.wendy\.monster` allows any subdomain; a compromised or attacker-registered subdomain gets full CORS access.

### `wendy/fragments.py` — 4/5

Loads `.md` fragment files with YAML frontmatter and executes `select` fields as Python expressions via `exec()`.

- **`exec()` on YAML-sourced code (line ~148):** `_SAFE_BUILTINS` whitelist is incomplete — `getattr`, `setattr`, and object introspection chains (e.g., `str.__class__.__bases__[0].__subclasses__()`) are not blocked. A crafted fragment can escape the sandbox.
- **Risk is real if Wendy can write fragments:** She can. Prompt injection → Wendy writes a malicious fragment → `exec()` escapes sandbox on next load.
- **No size limit on `select` expression (line ~107, 2000-char `_MAX_SELECT_LEN`):** Generous; complex expressions can be DoS vectors.

### `scripts/query_db.py` — 4/5

CLI tool that accepts user-supplied SQL queries against the wendy SQLite database.

- **Keyword filter bypass (line ~79):** Blocklist uses space-padded matching (`' DELETE '`), trivially bypassed via SQL comments (`SELECT/*DELETE*/1`) or CTEs (`WITH DELETE AS (...)`).
- **SQLite authorizer insufficient:** `mode=ro` URI and a keyword filter are not a real read-only guarantee; `PRAGMA` commands can still modify state or leak info.
- **`PRAGMA table_info({table['name']})` (line ~136):** Direct string interpolation of table names from `sqlite_master`; if the schema itself is tampered with, this is an injection point.

---

## Score 3 — Medium

### `wendy/discord_client.py` — 3/5

Main Discord event handler; spawns CLI subprocess and stores all incoming messages.

- **Attachment filename (line ~457):** `attachment.filename` is user-controlled and appended to a path. Handled safely by Python's `Path` (no traversal across components), but worth auditing if path handling ever changes.
- **Subprocess calls all use list form** (no `shell=True`) — safe as-is.

### `wendy/state.py` — 3/5

SQLite state management layer.

- **Dynamic SQL with `IN (?, ?, ...)` (lines ~360, 582):** Pattern is safe today but fragile; any future edit that accidentally drops the placeholder binding would create injection.
- **`session_id LIKE ?` with user prefix (line ~699):** `%` and `_` in user input are valid LIKE wildcards and will match unintended sessions. No input sanitization to escape these.
- **No size limits on `payload` JSON column:** A large notification payload could cause memory pressure during deserialization.

### `services/web/brain.py` — 3/5

Brain feed: reads stream logs and agent JSONL files and broadcasts over WebSocket.

- **Path traversal in agent log read (line ~514):** `agent_id` from URL used to build path `agent-{agent_id}.jsonl` without whitelist/regex validation; `../` sequences could read arbitrary files (unlikely to exist at that path, but structurally broken).
- **No bounds on reverse file read (lines ~116–145):** Manual backwards reading accumulates lines in memory; a large log file causes unbounded memory consumption.

### `scripts/secrets.py` — 3/5

Manages plaintext secrets on disk.

- **Secrets passed as CLI args (line ~102):** `sys.argv[3:]` — visible in `ps` output and shell history to any local user.
- **Plaintext at rest:** `chmod(0o600)` is the only protection; root processes and volume snapshots can read without restriction. No encryption.

### `scripts/webhooks.py` — 3/5

Generates and stores webhook tokens.

- **Weak entropy (line ~46):** `str(uuid.uuid4())` has 122 bits of entropy, which is acceptable but `secrets.token_urlsafe(32)` is the idiomatic choice for security tokens.
- **Token prefix logged (line ~61):** Truncated token shown in list output; if stdout is captured in logs, token prefixes leak.
- **`WENDY_WEB_URL` not validated (line ~21):** If overridden to an attacker domain, generated webhook URLs point externally.

### `scripts/cleanup_data_volume.py` — 3/5

One-off migration script with filesystem operations.

- **ReDoS in frontmatter regex (line ~61):** `re.match(r'^---\s*\n(.*?\n)---\s*\n', text, re.DOTALL)` — pathological input (many lines of whitespace) could cause catastrophic backtracking.
- **Path traversal via relative path (line ~247):** `rel.parent` not validated; a file path containing `..` components could escape the intended directory when constructing subdirectory targets.
- **Symlink following in `shutil.move()` (line ~54):** No symlink checks before moving; a planted symlink at destination could redirect the write.

---

## Score 2 — Low

### `wendy/cli.py` — 2/5

Builds and launches the Claude CLI subprocess with a constructed environment.

- `SENSITIVE_ENV_VARS` stripping and explicit re-injection of `CLAUDE_CODE_OAUTH_TOKEN` is correct.
- Regex parsing of log content (line ~379) is benign — no eval/exec.
- No significant vulnerabilities identified.

### `wendy/prompt.py` — 2/5

Assembles the 9-layer system prompt.

- Subprocess call to `bd list` (line ~213) uses hardcoded command, captured output, and runs as wendy user — safe.
- `BEADS_DIR` path is application-controlled; would become dangerous if ever user-influenced.
- String replacement of channel name / bot name into prompt text is a **prompt injection** risk at the LLM level (not a Python vulnerability), but worth noting.

### `wendy/config.py` — 2/5

Parses env vars and JSON channel config.

- **Unbounded JSON parse (line ~110):** `json.loads(WENDY_CHANNEL_CONFIG)` with no size limit; a very large value could cause memory pressure.
- No other significant concerns.

### `wendy/paths.py` — 2/5

Path helpers.

- `channel_dir(name)` (line ~53) does not validate `name` itself — relies on caller discipline. A `../` in a channel name would traverse.
- `ensure_channel_dirs()` (lines ~91–96) creates dirs + chowns without re-validating the name.

### `wendy/fragment_setup.py` — 2/5

Seeds fragment files from `config/` to data volume on startup.

- **TOCTOU on copy (lines ~38–42):** Checks `dest_file.exists()` then copies; race window allows symlink swap.
- **Symlink following in `rglob("*")` (line ~31):** Could follow symlinks out of `config/claude_fragments/` if planted.

---

## Score 1 — No Significant Attack Surface

| File | Notes |
|------|-------|
| `services/web/auth.py` | HMAC-SHA256 with `hmac.compare_digest` — well implemented |
| `wendy/enrichment.py` | Static prompt string constants only |
| `wendy/sessions.py` | Thin delegation wrapper, no direct I/O |
| `wendy/models.py` | Pure dataclass definitions |
| `wendy/__init__.py` / `wendy/__main__.py` | Entry point wiring only |

---

## Priority List (highest score first)

| Score | File |
|-------|------|
| 5 | `wendy/tasks.py` |
| 4 | `wendy/api_server.py` |
| 4 | `services/web/main.py` |
| 4 | `wendy/fragments.py` |
| 4 | `scripts/query_db.py` |
| 3 | `wendy/discord_client.py` |
| 3 | `wendy/state.py` |
| 3 | `services/web/brain.py` |
| 3 | `scripts/secrets.py` |
| 3 | `scripts/webhooks.py` |
| 3 | `scripts/cleanup_data_volume.py` |
| 2 | `wendy/cli.py` |
| 2 | `wendy/prompt.py` |
| 2 | `wendy/config.py` |
| 2 | `wendy/paths.py` |
| 2 | `wendy/fragment_setup.py` |
| 1 | `services/web/auth.py` |
| 1 | `wendy/enrichment.py` |
| 1 | `wendy/sessions.py` |
| 1 | `wendy/models.py` |
