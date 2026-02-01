"""Wendy Sites - Static HTML deployment service for wendy.monster.

This FastAPI service provides two main features:

1. **Static Site Deployment**: Deploy HTML/CSS/JS sites to subdomains of wendy.monster
   - POST /api/deploy - Deploy a tarball as a new site
   - GET /api/sites - List deployed sites
   - DELETE /api/sites/{name} - Remove a site
   - GET /{site_name}/{path} - Serve static files

2. **Brain Feed Dashboard**: Real-time visualization of Wendy's Claude Code session
   - GET / - Serve the dashboard HTML
   - POST /api/brain/auth - Authenticate with access code
   - WebSocket /ws/brain - Real-time event stream
   - GET /api/brain/stats - Session statistics
   - GET /api/brain/agents - List subagents
   - GET /api/brain/beads - Task queue

Security:
    - Site deployment requires DEPLOY_TOKEN in Authorization header
    - Brain feed requires BRAIN_ACCESS_CODE authentication
    - Tarball extraction has path traversal protection

Environment Variables:
    DEPLOY_TOKEN: Secret token for deployment API
    SITES_DIR: Directory to store deployed sites (default: /data/sites)
    BASE_URL: Base URL for site URLs (default: https://wendy.monster)
    BRAIN_ACCESS_CODE: Access code for brain feed
    BRAIN_SECRET: Secret for HMAC token signing
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import tarfile
import time
import uuid
from pathlib import Path

import auth
import brain
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

app = FastAPI(title="Wendy Sites", version="2.0.0")

# CORS middleware for avatar and other frontends
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost:\d+|127\.0\.0\.1:\d+|wendy\.monster|.*\.wendy\.monster)$",
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# =============================================================================
# Configuration
# =============================================================================

DEPLOY_TOKEN: str = os.environ.get("DEPLOY_TOKEN", "")
"""Secret token required for deployment API authentication."""

SITES_DIR: Path = Path(os.environ.get("SITES_DIR", "/data/sites"))
"""Directory where deployed sites are stored."""

MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024
"""Maximum upload size in bytes (50 MB)."""

SITE_NAME_PATTERN: re.Pattern = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$|^[a-z0-9]$")
"""Regex pattern for valid site names (lowercase alphanumeric with hyphens)."""

BASE_URL: str = os.environ.get("BASE_URL", "https://wendy.monster")
"""Base URL for constructing site URLs."""

STATIC_DIR: Path = Path(__file__).parent / "static"
"""Directory containing static files (brain dashboard HTML)."""

WENDY_DATA_DIR: Path = Path("/data/wendy")
"""Directory for Wendy bot data (shared with bot container)."""

WEBHOOKS_FILE: Path = WENDY_DATA_DIR / "secrets" / "webhooks.json"
"""JSON file containing webhook token configurations."""

WENDY_DB_PATH: Path = Path(os.getenv("WENDY_DB_PATH", "/data/wendy/wendy.db"))
"""Path to the shared SQLite database for webhook events."""

WEBHOOK_MAX_PAYLOAD: int = 1024 * 1024  # 1 MB
"""Maximum webhook payload size in bytes."""

# Rate limiting: track requests per token
_webhook_rate_limits: dict[str, list[float]] = {}
"""Token -> list of request timestamps for rate limiting."""

WEBHOOK_RATE_LIMIT: int = 10
"""Maximum requests per token per minute."""

# Ensure directories exist
SITES_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Application Lifecycle
# =============================================================================


@app.on_event("startup")
async def startup() -> None:
    """Start background tasks on application startup.

    Starts the brain feed file watcher if authentication is configured.
    """
    if auth.is_configured():
        brain.start_watcher()


# =============================================================================
# Brain Feed Models
# =============================================================================


class BrainAuthRequest(BaseModel):
    """Request body for brain feed authentication.

    Attributes:
        code: Access code to validate.
    """

    code: str


class BrainAuthResponse(BaseModel):
    """Response from successful brain authentication.

    Attributes:
        token: HMAC-signed token for WebSocket authentication.
    """

    token: str


# =============================================================================
# Brain Feed Endpoints
# =============================================================================


@app.get("/", response_class=HTMLResponse)
async def serve_brain_page() -> HTMLResponse:
    """Serve the brain feed dashboard HTML at the root URL."""
    brain_html = STATIC_DIR / "brain" / "index.html"
    if brain_html.exists():
        return HTMLResponse(brain_html.read_text())
    return HTMLResponse("<h1>Brain feed not configured</h1>", status_code=503)


# =============================================================================
# Avatar Static Files
# =============================================================================

AVATAR_DIR: Path = STATIC_DIR / "avatar"
"""Directory containing avatar static files."""


@app.get("/avatar/")
async def serve_avatar_root() -> FileResponse:
    """Serve avatar index.html at /avatar/."""
    return FileResponse(AVATAR_DIR / "index.html")


@app.get("/avatar/{path:path}")
async def serve_avatar_files(path: str) -> FileResponse:
    """Serve avatar static files (JS, CSS, etc)."""
    file_path = (AVATAR_DIR / path).resolve()

    # Security: ensure we're still within avatar directory
    if not str(file_path).startswith(str(AVATAR_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not a file")

    # Set correct content type for JS modules
    media_type = None
    if path.endswith(".js"):
        media_type = "application/javascript"
    elif path.endswith(".css"):
        media_type = "text/css"

    return FileResponse(file_path, media_type=media_type)


@app.get("/api/brain/stats")
async def brain_stats() -> dict:
    """Get current brain feed statistics (context, costs, activity)."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")
    return brain.get_stats()


USAGE_DATA_FILE: Path = Path("/data/wendy/usage_data.json")
"""Path to usage statistics file written by orchestrator."""


@app.get("/api/brain/usage")
async def brain_usage() -> dict:
    """Get Claude Code usage statistics (session and weekly limits)."""
    import json
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")

    if not USAGE_DATA_FILE.exists():
        return {
            "available": False,
            "message": "Usage data not available yet"
        }

    try:
        data = json.loads(USAGE_DATA_FILE.read_text())
        return {
            "available": True,
            "session_percent": data.get("session_percent", 0),
            "session_resets": data.get("session_resets", ""),
            "week_all_percent": data.get("week_all_percent", 0),
            "week_all_resets": data.get("week_all_resets", ""),
            "week_sonnet_percent": data.get("week_sonnet_percent", 0),
            "week_sonnet_resets": data.get("week_sonnet_resets", ""),
            "updated_at": data.get("updated_at", ""),
        }
    except Exception as e:
        return {
            "available": False,
            "message": f"Error reading usage data: {e}"
        }


@app.get("/api/brain/agents")
async def brain_agents() -> dict:
    """List all active and completed subagents from the current session."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")
    return {"agents": brain.list_agents()}


@app.get("/api/brain/agents/{agent_id}")
async def brain_agent_events(agent_id: str, limit: int = 50) -> dict:
    """Get recent events from a specific subagent's log file."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")
    events = brain.get_agent_events(agent_id, limit)
    return {"agent_id": agent_id, "events": events}


@app.get("/api/brain/beads")
async def brain_beads() -> dict:
    """List all tasks from the beads queue (open, in_progress, closed)."""
    import json
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")

    jsonl_path = Path("/data/wendy/coding/.beads/issues.jsonl")
    beads = []

    if jsonl_path.exists():
        try:
            # Parse JSONL - later lines update earlier ones (append-only log)
            issues_by_id = {}
            for line in jsonl_path.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    issue_id = data.get("id")
                    if issue_id:
                        issues_by_id[issue_id] = data
                except json.JSONDecodeError:
                    continue

            for issue_id, data in issues_by_id.items():
                beads.append({
                    "id": issue_id,
                    "title": data.get("title", "Untitled"),
                    "status": data.get("status", "open"),
                    "priority": data.get("priority", 2),
                    "created": data.get("created"),
                    "updated": data.get("updated", data.get("created")),  # Track last update
                    "labels": data.get("labels", []),
                })
        except OSError:
            pass

    # Sort: in_progress first, then open by priority, then closed/tombstone by recency
    # tombstone = archived/compacted tasks (show fewer of these)
    status_order = {"in_progress": 0, "open": 1, "closed": 2, "tombstone": 3}

    def sort_key(b):
        status_rank = status_order.get(b["status"], 4)
        if b["status"] in ("closed", "tombstone"):
            # Sort by updated time descending (most recent first)
            updated = b.get("updated") or b.get("created") or ""
            return (status_rank, 0, updated)
        else:
            # Open/in_progress: sort by priority (lower = higher priority)
            return (status_rank, b.get("priority", 2), "")

    beads.sort(key=sort_key)

    # Separate by status and limit old tasks
    MAX_CLOSED = 10
    MAX_TOMBSTONE = 5  # Show fewer archived tasks

    active = [b for b in beads if b["status"] in ("in_progress", "open")]
    closed = [b for b in beads if b["status"] == "closed"]
    tombstone = [b for b in beads if b["status"] == "tombstone"]

    # Sort closed/tombstone by recency (most recent first)
    closed.sort(key=lambda b: b.get("updated") or b.get("created") or "", reverse=True)
    tombstone.sort(key=lambda b: b.get("updated") or b.get("created") or "", reverse=True)

    beads = active + closed[:MAX_CLOSED] + tombstone[:MAX_TOMBSTONE]

    return {"beads": beads}


@app.get("/api/brain/beads/{task_id}/log")
async def brain_task_log(task_id: str, offset: int = 0) -> dict:
    """Get orchestrator log output for a running or completed task."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")

    logs_dir = Path("/data/wendy/orchestrator_logs")
    if not logs_dir.exists():
        return {"task_id": task_id, "log": "", "offset": 0, "complete": False}

    # Find log file for this task
    log_files = list(logs_dir.glob(f"agent_{task_id}_*.log"))
    if not log_files:
        return {"task_id": task_id, "log": "", "offset": 0, "complete": False}

    # Use the most recent log file for this task
    log_file = max(log_files, key=lambda f: f.stat().st_mtime)

    try:
        content = log_file.read_text()
        # Return content from offset
        new_content = content[offset:] if offset < len(content) else ""
        new_offset = len(content)

        # Check if task is complete (look for completion markers)
        complete = "=== TASK COMPLETE ===" in content or "=== TASK FAILED ===" in content

        return {
            "task_id": task_id,
            "log": new_content,
            "offset": new_offset,
            "complete": complete,
        }
    except OSError:
        return {"task_id": task_id, "log": "", "offset": 0, "complete": False}


@app.post("/api/brain/auth", response_model=BrainAuthResponse)
async def brain_authenticate(request: BrainAuthRequest) -> BrainAuthResponse:
    """Validate access code and return signed authentication token."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")

    if not auth.verify_code(request.code):
        raise HTTPException(status_code=401, detail="Invalid code")

    return BrainAuthResponse(token=auth.generate_token())


@app.websocket("/ws/brain")
async def brain_websocket(websocket: WebSocket, token: str = Query("")) -> None:
    """WebSocket endpoint for real-time brain feed event streaming.

    Validates token, sends recent history, then streams new events.
    Sends periodic pings to detect dead connections.
    """
    # Validate token
    if not auth.verify_token(token):
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    # Check capacity
    if not await brain.add_client(websocket):
        await websocket.close(code=4002, reason="Server at capacity")
        return

    await websocket.accept()

    try:
        # Send recent history
        for event in brain.get_recent_events():
            await websocket.send_text(event)

        # Keep connection alive, handle client messages (ping/pong)
        while True:
            try:
                # Wait for any message (keepalive)
                await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except TimeoutError:
                # Send ping to check if client is alive
                await websocket.send_text('{"type":"ping"}')

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        brain.remove_client(websocket)


# =============================================================================
# Webhook Endpoints
# =============================================================================


def _load_webhooks() -> dict:
    """Load webhook configurations from file."""
    if not WEBHOOKS_FILE.exists():
        return {}
    try:
        return json.loads(WEBHOOKS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _validate_webhook_token(token: str) -> dict | None:
    """Validate a webhook token and return its config, or None if invalid."""
    webhooks = _load_webhooks()
    for name, config in webhooks.items():
        if config.get("token") == token:
            return {"name": name, **config}
    return None


def _check_rate_limit(token: str) -> bool:
    """Check if token is within rate limit. Returns True if allowed."""
    now = time.time()
    minute_ago = now - 60

    # Clean old entries
    if token in _webhook_rate_limits:
        _webhook_rate_limits[token] = [
            ts for ts in _webhook_rate_limits[token] if ts > minute_ago
        ]
    else:
        _webhook_rate_limits[token] = []

    # Check limit
    if len(_webhook_rate_limits[token]) >= WEBHOOK_RATE_LIMIT:
        return False

    # Record this request
    _webhook_rate_limits[token].append(now)
    return True


def _detect_webhook_source(headers: dict) -> tuple[str, str]:
    """Detect webhook source from headers.

    Returns:
        Tuple of (source_name, event_type).
    """
    # GitHub
    if "x-github-event" in headers:
        return "github", headers["x-github-event"]

    # GitLab
    if "x-gitlab-event" in headers:
        return "gitlab", headers["x-gitlab-event"]

    # Bitbucket
    if "x-event-key" in headers:
        return "bitbucket", headers["x-event-key"]

    # Generic
    return "webhook", "unknown"


def _format_github_summary(event_type: str, payload: dict) -> str:
    """Format GitHub webhook payload into human-readable summary."""
    repo = payload.get("repository", {}).get("full_name", "unknown repo")
    sender = payload.get("sender", {}).get("login", "someone")

    if event_type == "push":
        commits = payload.get("commits", [])
        branch = payload.get("ref", "").replace("refs/heads/", "")
        count = len(commits)
        if count == 1:
            msg = commits[0].get("message", "").split("\n")[0][:50]
            return f"{sender} pushed to {branch} in {repo}: \"{msg}\""
        return f"{sender} pushed {count} commits to {branch} in {repo}"

    if event_type == "pull_request":
        action = payload.get("action", "updated")
        pr = payload.get("pull_request", {})
        title = pr.get("title", "")[:50]
        number = pr.get("number", "?")
        return f"{sender} {action} PR #{number} in {repo}: \"{title}\""

    if event_type == "issues":
        action = payload.get("action", "updated")
        issue = payload.get("issue", {})
        title = issue.get("title", "")[:50]
        number = issue.get("number", "?")
        return f"{sender} {action} issue #{number} in {repo}: \"{title}\""

    if event_type == "issue_comment":
        action = payload.get("action", "created")
        issue = payload.get("issue", {})
        number = issue.get("number", "?")
        return f"{sender} {action} comment on #{number} in {repo}"

    if event_type == "release":
        action = payload.get("action", "published")
        release = payload.get("release", {})
        tag = release.get("tag_name", "?")
        return f"{sender} {action} release {tag} in {repo}"

    if event_type == "star":
        action = payload.get("action", "created")
        if action == "created":
            return f"{sender} starred {repo}"
        return f"{sender} unstarred {repo}"

    if event_type == "fork":
        forkee = payload.get("forkee", {}).get("full_name", "unknown")
        return f"{sender} forked {repo} to {forkee}"

    if event_type == "ping":
        return f"GitHub ping from {repo} - webhook configured successfully"

    # Generic fallback
    return f"GitHub {event_type} event from {sender} in {repo}"


def _format_gitlab_summary(event_type: str, payload: dict) -> str:
    """Format GitLab webhook payload into human-readable summary."""
    project = payload.get("project", {}).get("path_with_namespace", "unknown")
    user = payload.get("user", {}).get("username", "someone")

    if "Push Hook" in event_type:
        commits = payload.get("commits", [])
        branch = payload.get("ref", "").replace("refs/heads/", "")
        count = len(commits)
        return f"{user} pushed {count} commit(s) to {branch} in {project}"

    if "Merge Request Hook" in event_type:
        attrs = payload.get("object_attributes", {})
        action = attrs.get("action", "updated")
        title = attrs.get("title", "")[:50]
        iid = attrs.get("iid", "?")
        return f"{user} {action} MR !{iid} in {project}: \"{title}\""

    return f"GitLab {event_type} from {user} in {project}"


def _format_webhook_summary(source: str, event_type: str, payload: dict) -> str:
    """Format webhook payload into human-readable summary."""
    if source == "github":
        return _format_github_summary(event_type, payload)
    if source == "gitlab":
        return _format_gitlab_summary(event_type, payload)
    # Generic summary
    return f"Webhook event: {event_type}"


def _write_webhook_event(
    channel_id: str,
    channel_name: str,
    source: str,
    event_type: str,
    summary: str,
    payload: dict,
) -> None:
    """Write a webhook notification to SQLite for the bot to pick up."""
    try:
        channel_id_int = int(channel_id)
    except ValueError:
        return

    # Build payload with event metadata
    notification_payload = {
        "event_type": event_type,
        "raw": payload,
    }
    payload_str = json.dumps(notification_payload)

    try:
        WENDY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(WENDY_DB_PATH, timeout=30.0) as conn:
            # DUPLICATE SCHEMA WARNING: This is a partial copy from bot/state_manager.py
            # Primary source of truth is bot/state_manager.py._init_schema()
            # If you modify notifications table, update both locations!
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    channel_id INTEGER,
                    title TEXT NOT NULL,
                    payload TEXT,
                    seen_by_wendy INTEGER DEFAULT 0,
                    seen_by_proxy INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                """
                INSERT INTO notifications (type, source, channel_id, title, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("webhook", source, channel_id_int, summary, payload_str)
            )
            # Cleanup old notifications (keep last 100)
            conn.execute("""
                DELETE FROM notifications
                WHERE id NOT IN (
                    SELECT id FROM notifications
                    ORDER BY created_at DESC
                    LIMIT 100
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"Failed to write webhook notification: {e}")


@app.post("/webhook/{token}")
async def receive_webhook(token: str, request: Request) -> JSONResponse:
    """Receive a webhook POST and queue it for the bot.

    Returns 404 for invalid tokens (prevents enumeration).
    Returns 429 if rate limited.
    """
    # Validate token
    config = _validate_webhook_token(token)
    if not config:
        # Return 404 to prevent token enumeration
        raise HTTPException(status_code=404, detail="Not found")

    # Check rate limit
    if not _check_rate_limit(token):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Check payload size
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > WEBHOOK_MAX_PAYLOAD:
        raise HTTPException(status_code=413, detail="Payload too large")

    # Parse payload
    try:
        body = await request.body()
        if len(body) > WEBHOOK_MAX_PAYLOAD:
            raise HTTPException(status_code=413, detail="Payload too large")

        # Try to parse as JSON
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"raw": body.decode("utf-8", errors="replace")}
    except Exception:
        payload = {}

    # Detect source and event type
    headers = {k.lower(): v for k, v in request.headers.items()}
    source, event_type = _detect_webhook_source(headers)

    # Format summary
    summary = _format_webhook_summary(source, event_type, payload)

    # Write event for bot
    _write_webhook_event(
        channel_id=config["channel_id"],
        channel_name=config["name"],
        source=source,
        event_type=event_type,
        summary=summary,
        payload=payload,
    )

    return JSONResponse({
        "success": True,
        "message": "Webhook received",
        "event_id": str(uuid.uuid4()),
    })


@app.get("/webhook/{token}/test")
async def test_webhook(token: str) -> JSONResponse:
    """Validate a webhook token without triggering an event.

    Returns 404 for invalid tokens.
    """
    config = _validate_webhook_token(token)
    if not config:
        raise HTTPException(status_code=404, detail="Not found")

    return JSONResponse({
        "valid": True,
        "channel": config["name"],
    })


# =============================================================================
# Health Check
# =============================================================================


@app.get("/health")
async def health() -> dict:
    """Health check endpoint for monitoring and load balancers."""
    return {
        "status": "healthy",
        "sites_count": len(list(SITES_DIR.iterdir())) if SITES_DIR.exists() else 0,
        "brain_clients": brain.client_count(),
        "brain_configured": auth.is_configured(),
    }


# =============================================================================
# Site Deployment Helpers
# =============================================================================


def verify_deploy_token(authorization: str | None) -> None:
    """Verify the Authorization header contains a valid deploy token.

    Args:
        authorization: Authorization header value (e.g., "Bearer <token>").

    Raises:
        HTTPException 401: Missing authorization.
        HTTPException 403: Invalid token.
        HTTPException 500: Server not configured.
    """
    if not DEPLOY_TOKEN:
        raise HTTPException(status_code=500, detail="Server not configured with deploy token")

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    # Support both "Bearer <token>" and raw token
    token = authorization
    if authorization.startswith("Bearer "):
        token = authorization[7:]

    if token != DEPLOY_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid deploy token")


def validate_site_name(name: str) -> None:
    """Validate that site name meets requirements.

    Args:
        name: Proposed site name.

    Raises:
        HTTPException 400: Invalid or reserved site name.
    """
    if not name:
        raise HTTPException(status_code=400, detail="Site name is required")

    if not SITE_NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=400,
            detail="Site name must be lowercase alphanumeric with hyphens, 1-32 chars"
        )

    # Reserved names
    reserved = {"api", "health", "admin", "static", "assets", "ws", "brain"}
    if name in reserved:
        raise HTTPException(status_code=400, detail=f"Site name '{name}' is reserved")


def safe_extract_tarball(tar_path: Path, dest_dir: Path) -> None:
    """Safely extract tarball with path traversal protection.

    Args:
        tar_path: Path to the tarball file.
        dest_dir: Destination directory.

    Raises:
        HTTPException 400: Tarball contains absolute paths or path traversal.
    """
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            # Check for path traversal attempts
            member_path = Path(member.name)
            if member_path.is_absolute():
                raise HTTPException(status_code=400, detail="Tarball contains absolute paths")

            # Resolve the final path and ensure it's within dest_dir
            final_path = (dest_dir / member.name).resolve()
            if not str(final_path).startswith(str(dest_dir.resolve())):
                raise HTTPException(status_code=400, detail="Tarball contains path traversal")

            # Skip potentially dangerous files
            if member.name.startswith(".") and member.name != ".":
                continue

        # Extract all files
        tar.extractall(dest_dir, filter="data")


# =============================================================================
# Site Deployment Endpoints
# =============================================================================


@app.post("/api/deploy")
async def deploy_site(
    name: str = Form(...),
    files: UploadFile = File(...),
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Deploy a static site from a tarball archive.

    Extracts the tarball to SITES_DIR/{name}/ and verifies index.html exists.
    """
    verify_deploy_token(authorization)
    validate_site_name(name)

    # Check file size
    content = await files.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Upload too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB"
        )

    # Save tarball temporarily
    tmp_tar = Path(f"/tmp/upload_{name}.tar.gz")
    try:
        tmp_tar.write_bytes(content)

        # Verify it's a valid tarball
        if not tarfile.is_tarfile(tmp_tar):
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid tarball")

        # Prepare site directory
        site_dir = SITES_DIR / name
        if site_dir.exists():
            shutil.rmtree(site_dir)
        site_dir.mkdir(parents=True)

        # Extract tarball
        safe_extract_tarball(tmp_tar, site_dir)

        # Verify index.html exists
        index_file = site_dir / "index.html"
        if not index_file.exists():
            # Check for index.htm as fallback
            index_htm = site_dir / "index.htm"
            if not index_htm.exists():
                shutil.rmtree(site_dir)
                raise HTTPException(status_code=400, detail="Site must contain index.html")

        url = f"{BASE_URL}/{name}/"
        return JSONResponse({
            "success": True,
            "url": url,
            "message": f"Site deployed successfully at {url}"
        })

    finally:
        if tmp_tar.exists():
            tmp_tar.unlink()


@app.get("/api/sites")
async def list_sites(authorization: str | None = Header(None)) -> dict:
    """List all deployed sites with their URLs."""
    verify_deploy_token(authorization)

    sites = []
    for site_dir in SITES_DIR.iterdir():
        if site_dir.is_dir():
            sites.append({
                "name": site_dir.name,
                "url": f"{BASE_URL}/{site_dir.name}/",
            })

    return {"sites": sites}


@app.delete("/api/sites/{name}")
async def delete_site(name: str, authorization: str | None = Header(None)) -> dict:
    """Delete a deployed site by name."""
    verify_deploy_token(authorization)
    validate_site_name(name)

    site_dir = SITES_DIR / name
    if not site_dir.exists():
        raise HTTPException(status_code=404, detail=f"Site '{name}' not found")

    shutil.rmtree(site_dir)
    return {"success": True, "message": f"Site '{name}' deleted"}


# =============================================================================
# Static File Serving
# =============================================================================


@app.get("/{site_name}/{path:path}")
async def serve_site_file(site_name: str, path: str = "") -> FileResponse:
    """Serve static files from a deployed site with security checks."""
    site_dir = SITES_DIR / site_name

    if not site_dir.exists():
        raise HTTPException(status_code=404, detail="Site not found")

    # Default to index.html
    if not path or path.endswith("/"):
        path = path.rstrip("/") + "/index.html" if path else "index.html"

    file_path = (site_dir / path).resolve()

    # Security: ensure we're still within the site directory
    if not str(file_path).startswith(str(site_dir.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        # Try adding .html extension
        html_path = file_path.with_suffix(".html")
        if html_path.exists():
            file_path = html_path
        else:
            raise HTTPException(status_code=404, detail="File not found")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not a file")

    return FileResponse(file_path)


@app.get("/{site_name}")
async def serve_site_root(site_name: str) -> FileResponse:
    """Serve a site's index.html directly (without trailing slash)."""
    return FileResponse(SITES_DIR / site_name / "index.html")
