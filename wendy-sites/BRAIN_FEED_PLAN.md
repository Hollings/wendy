# Wendy Brain Feed - Implementation Plan

## Overview

Add a real-time visualization of Wendy's Claude Code session to wendy.monster root page. Users can watch her "thinking" in real-time as she processes Discord messages. Access is protected by a simple code word that friends know.

## Data Source

wendy-bot already writes Claude Code stream events to `/data/wendy/stream.jsonl`:

```json
{"ts": 1704067200000, "channel_id": 123456, "event": {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me check..."}]}}}
{"ts": 1704067201000, "channel_id": 123456, "event": {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "curl ..."}}]}}}
```

Event types from Claude Code stream-json format:
- `system` (subtype: `init`) - Session started
- `assistant` - Wendy's responses (text blocks, tool_use blocks)
- `user` - Tool results (tool_result blocks)
- `result` - Session complete with stats

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     wendy.monster                            │
├─────────────────────────────────────────────────────────────┤
│  GET  /                → Brain page (code prompt or feed)    │
│  POST /api/brain/auth  → Validate code, return token         │
│  WS   /ws/brain?token= → Real-time event stream (authed)     │
│  GET  /{site}/...      → Existing static site serving        │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ tails (read-only)
                              ▼
                    /data/wendy/stream.jsonl
                              ▲
                              │ writes
                    ┌─────────┴─────────┐
                    │    wendy-bot      │
                    │  (Claude CLI)     │
                    └───────────────────┘
```

## Authentication Flow

```
User visits wendy.monster/ or wendy.monster/?key=wendyiscool
                              │
                              ▼
                    ┌───────────────────┐
                    │ Has valid token   │
                    │ in localStorage?  │
                    └─────────┬─────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
                   yes                  no
                    │                   │
                    ▼                   ▼
             Connect to WS      ┌───────────────────┐
                    │           │ ?key= in URL?     │
                    │           └─────────┬─────────┘
                    │                     │
                    │           ┌─────────┴─────────┐
                    │           │                   │
                    │          yes                  no
                    │           │                   │
                    │           ▼                   ▼
                    │    POST /api/brain/auth   Show code
                    │    with key value         prompt
                    │           │                   │
                    │           │           User types code
                    │           │                   │
                    │           └─────────┬─────────┘
                    │                     │
                    │                     ▼
                    │           ┌───────────────────┐
                    │           │ POST /api/brain/  │
                    │           │ auth {code: "..."}│
                    │           └─────────┬─────────┘
                    │                     │
                    │           ┌─────────┴─────────┐
                    │           │                   │
                    │        valid              invalid
                    │           │                   │
                    │           ▼                   ▼
                    │    Store token in LS    Show "nope"
                    │    Clean URL if needed
                    │    Connect to WS
                    │           │
                    └───────────┴──────────────────────────┐
                                                          │
                                                          ▼
                                              ┌───────────────────┐
                                              │ WS /ws/brain      │
                                              │ ?token=xxx        │
                                              └─────────┬─────────┘
                                                        │
                                              ┌─────────┴─────────┐
                                              │                   │
                                           valid              invalid
                                              │                   │
                                              ▼                   ▼
                                         Stream events      Close 4001
                                                           Clear localStorage
                                                           Show prompt
```

## Implementation Steps

### Step 1: Update Docker Compose for wendy-sites

**File:** `services/wendy-sites/deploy/docker-compose.yml`

Add read-only bind mount for wendy data. Note: wendy-bot uses a named Docker volume, so we mount from the Docker volume path:

```yaml
services:
  backend:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    ports:
      - "0.0.0.0:8910:8000"
    volumes:
      - sites_data:/data/sites
      - /var/lib/docker/volumes/wendy_data/_data:/data/wendy:ro
    environment:
      - BRAIN_ACCESS_CODE=${BRAIN_ACCESS_CODE}
      - BRAIN_SECRET=${BRAIN_SECRET}
    restart: unless-stopped

volumes:
  sites_data:
```

**Alternative:** Change wendy-bot to use a bind mount at `/srv/wendy-bot/data` instead of a named volume (cleaner long-term).

### Step 2: Add Dependencies

**File:** `services/wendy-sites/backend/requirements.txt`

```
fastapi>=0.109.0
uvicorn>=0.27.0
python-multipart>=0.0.6
watchfiles>=0.21.0
```

### Step 3: Add Authentication Module

**File:** `services/wendy-sites/backend/auth.py`

```python
"""Brain feed authentication using HMAC-signed tokens."""

import hashlib
import hmac
import os
import time

BRAIN_ACCESS_CODE = os.environ.get("BRAIN_ACCESS_CODE", "")
BRAIN_SECRET = os.environ.get("BRAIN_SECRET", "")
TOKEN_LIFETIME = 60 * 60 * 24 * 30  # 30 days


def is_configured() -> bool:
    """Check if brain auth is configured."""
    return bool(BRAIN_ACCESS_CODE and BRAIN_SECRET)


def verify_code(code: str) -> bool:
    """Verify the access code."""
    if not BRAIN_ACCESS_CODE:
        return False
    return hmac.compare_digest(code, BRAIN_ACCESS_CODE)


def generate_token() -> str:
    """Generate a signed token with expiry."""
    expires = int(time.time()) + TOKEN_LIFETIME
    payload = f"brain:{expires}"
    signature = hmac.new(
        BRAIN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    return f"{expires}:{signature}"


def verify_token(token: str) -> bool:
    """Verify token signature and expiry."""
    if not BRAIN_SECRET:
        return False
    try:
        expires_str, signature = token.split(":", 1)
        expires = int(expires_str)

        # Check expiry
        if time.time() > expires:
            return False

        # Check signature
        payload = f"brain:{expires}"
        expected = hmac.new(
            BRAIN_SECRET.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()[:16]

        return hmac.compare_digest(signature, expected)
    except (ValueError, AttributeError):
        return False
```

### Step 4: Add Brain Feed Module

**File:** `services/wendy-sites/backend/brain.py`

```python
"""Brain feed - real-time stream of Wendy's Claude Code session."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Set

from fastapi import WebSocket
from watchfiles import awatch, Change

_LOG = logging.getLogger(__name__)

STREAM_FILE = Path("/data/wendy/stream.jsonl")
MAX_HISTORY = 50
MAX_CLIENTS = 100

connected_clients: Set[WebSocket] = set()
_watcher_task: asyncio.Task | None = None


def get_recent_events(n: int = MAX_HISTORY) -> list[str]:
    """Get last N events from stream file efficiently."""
    if not STREAM_FILE.exists():
        return []

    try:
        # Read file in reverse to get last N lines efficiently
        with open(STREAM_FILE, "rb") as f:
            # Seek to end
            f.seek(0, 2)
            file_size = f.tell()

            if file_size == 0:
                return []

            # Read chunks from end until we have enough lines
            chunk_size = 8192
            lines = []
            position = file_size

            while position > 0 and len(lines) <= n:
                read_size = min(chunk_size, position)
                position -= read_size
                f.seek(position)
                chunk = f.read(read_size).decode("utf-8", errors="replace")

                # Split and accumulate lines
                chunk_lines = chunk.split("\n")
                if lines:
                    # Merge with previous partial line
                    chunk_lines[-1] += lines[0]
                    lines = chunk_lines + lines[1:]
                else:
                    lines = chunk_lines

            # Return last N non-empty lines
            return [l.strip() for l in lines[-n:] if l.strip()]

    except Exception as e:
        _LOG.error("Failed to read recent events: %s", e)
        return []


async def broadcast(message: str) -> None:
    """Send message to all connected clients."""
    if not connected_clients:
        return

    dead = set()
    tasks = []

    for ws in connected_clients:
        try:
            tasks.append(ws.send_text(message))
        except Exception:
            dead.add(ws)

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ws, result in zip(connected_clients, results):
            if isinstance(result, Exception):
                dead.add(ws)

    connected_clients.difference_update(dead)

    if dead:
        _LOG.info("Removed %d dead connections, %d remaining", len(dead), len(connected_clients))


async def tail_stream() -> None:
    """Watch stream.jsonl and broadcast new events."""
    _LOG.info("Starting brain feed watcher...")

    while True:
        try:
            # Wait for file to exist
            while not STREAM_FILE.exists():
                _LOG.debug("Waiting for stream file to exist...")
                await asyncio.sleep(5)

            pos = STREAM_FILE.stat().st_size
            _LOG.info("Stream file found, starting from position %d", pos)

            async for changes in awatch(STREAM_FILE):
                for change_type, _ in changes:
                    if change_type == Change.deleted:
                        _LOG.warning("Stream file deleted, waiting for recreation...")
                        break

                    try:
                        current_size = STREAM_FILE.stat().st_size

                        # Handle file truncation (wendy-bot trims to 5000 lines)
                        if current_size < pos:
                            _LOG.info("File truncated, resetting position from %d to 0", pos)
                            pos = 0

                        if current_size <= pos:
                            continue

                        with open(STREAM_FILE, "r") as f:
                            f.seek(pos)
                            new_lines = f.readlines()
                            pos = f.tell()

                        for line in new_lines:
                            line = line.strip()
                            if line:
                                await broadcast(line)

                    except FileNotFoundError:
                        _LOG.warning("Stream file disappeared during read")
                        pos = 0
                        break

        except Exception as e:
            _LOG.exception("Watcher error: %s", e)
            await asyncio.sleep(5)


async def add_client(ws: WebSocket) -> bool:
    """Add a client connection. Returns False if at capacity."""
    if len(connected_clients) >= MAX_CLIENTS:
        return False
    connected_clients.add(ws)
    _LOG.info("Client connected, total: %d", len(connected_clients))
    return True


def remove_client(ws: WebSocket) -> None:
    """Remove a client connection."""
    connected_clients.discard(ws)
    _LOG.info("Client disconnected, total: %d", len(connected_clients))


def client_count() -> int:
    """Get number of connected clients."""
    return len(connected_clients)


def start_watcher() -> None:
    """Start the file watcher background task."""
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        _watcher_task = asyncio.create_task(tail_stream())
        _LOG.info("Brain feed watcher started")
```

### Step 5: Update Main Application

**File:** `services/wendy-sites/backend/main.py`

```python
"""
Wendy Sites - Static HTML deployment service for wendy.monster

Also serves the Brain Feed - real-time visualization of Wendy's Claude Code session.
"""

import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Header, UploadFile, WebSocket, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from . import auth, brain

app = FastAPI(title="Wendy Sites", version="2.0.0")

# Configuration
DEPLOY_TOKEN = os.environ.get("DEPLOY_TOKEN", "")
SITES_DIR = Path(os.environ.get("SITES_DIR", "/data/sites"))
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
SITE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$|^[a-z0-9]$")
BASE_URL = os.environ.get("BASE_URL", "https://wendy.monster")

# Static files directory
STATIC_DIR = Path(__file__).parent / "static"

# Ensure directories exist
SITES_DIR.mkdir(parents=True, exist_ok=True)


# ==================== Startup ====================

@app.on_event("startup")
async def startup():
    """Start background tasks."""
    if auth.is_configured():
        brain.start_watcher()


# ==================== Brain Feed ====================

class BrainAuthRequest(BaseModel):
    code: str


class BrainAuthResponse(BaseModel):
    token: str


@app.get("/", response_class=HTMLResponse)
async def serve_brain_page():
    """Serve the brain feed page."""
    brain_html = STATIC_DIR / "brain" / "index.html"
    if brain_html.exists():
        return HTMLResponse(brain_html.read_text())
    return HTMLResponse("<h1>Brain feed not configured</h1>", status_code=503)


@app.post("/api/brain/auth", response_model=BrainAuthResponse)
async def brain_authenticate(request: BrainAuthRequest):
    """Validate access code and return token."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")

    if not auth.verify_code(request.code):
        raise HTTPException(status_code=401, detail="Invalid code")

    return BrainAuthResponse(token=auth.generate_token())


@app.websocket("/ws/brain")
async def brain_websocket(websocket: WebSocket, token: str = Query("")):
    """WebSocket endpoint for brain feed."""
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
            except asyncio.TimeoutError:
                # Send ping to check if client is alive
                await websocket.send_text('{"type":"ping"}')

    except WebSocketDisconnect:
        pass
    except Exception as e:
        pass
    finally:
        brain.remove_client(websocket)


# ==================== Health Check ====================

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "sites_count": len(list(SITES_DIR.iterdir())) if SITES_DIR.exists() else 0,
        "brain_clients": brain.client_count(),
        "brain_configured": auth.is_configured(),
    }


# ==================== Site Deployment (existing code) ====================

def verify_token(authorization: Optional[str]) -> None:
    """Verify the deploy token."""
    if not DEPLOY_TOKEN:
        raise HTTPException(status_code=500, detail="Server not configured with deploy token")

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    token = authorization
    if authorization.startswith("Bearer "):
        token = authorization[7:]

    if token != DEPLOY_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid deploy token")


def validate_site_name(name: str) -> None:
    """Validate site name format."""
    if not name:
        raise HTTPException(status_code=400, detail="Site name is required")

    if not SITE_NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=400,
            detail="Site name must be lowercase alphanumeric with hyphens, 1-32 chars"
        )

    reserved = {"api", "health", "admin", "static", "assets", "ws", "brain"}
    if name in reserved:
        raise HTTPException(status_code=400, detail=f"Site name '{name}' is reserved")


def safe_extract_tarball(tar_path: Path, dest_dir: Path) -> None:
    """Safely extract tarball with path traversal protection."""
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute():
                raise HTTPException(status_code=400, detail="Tarball contains absolute paths")

            final_path = (dest_dir / member.name).resolve()
            if not str(final_path).startswith(str(dest_dir.resolve())):
                raise HTTPException(status_code=400, detail="Tarball contains path traversal")

            if member.name.startswith(".") and member.name != ".":
                continue

        tar.extractall(dest_dir, filter="data")


@app.post("/api/deploy")
async def deploy_site(
    name: str = Form(...),
    files: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Deploy a site from a tarball."""
    verify_token(authorization)
    validate_site_name(name)

    content = await files.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Upload too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB"
        )

    tmp_tar = Path(f"/tmp/upload_{name}.tar.gz")
    try:
        tmp_tar.write_bytes(content)

        if not tarfile.is_tarfile(tmp_tar):
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid tarball")

        site_dir = SITES_DIR / name
        if site_dir.exists():
            shutil.rmtree(site_dir)
        site_dir.mkdir(parents=True)

        safe_extract_tarball(tmp_tar, site_dir)

        index_file = site_dir / "index.html"
        if not index_file.exists():
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
async def list_sites(authorization: Optional[str] = Header(None)):
    """List all deployed sites."""
    verify_token(authorization)

    sites = []
    for site_dir in SITES_DIR.iterdir():
        if site_dir.is_dir():
            sites.append({
                "name": site_dir.name,
                "url": f"{BASE_URL}/{site_dir.name}/",
            })

    return {"sites": sites}


@app.delete("/api/sites/{name}")
async def delete_site(name: str, authorization: Optional[str] = Header(None)):
    """Delete a deployed site."""
    verify_token(authorization)
    validate_site_name(name)

    site_dir = SITES_DIR / name
    if not site_dir.exists():
        raise HTTPException(status_code=404, detail=f"Site '{name}' not found")

    shutil.rmtree(site_dir)
    return {"success": True, "message": f"Site '{name}' deleted"}


# ==================== Static File Serving ====================

@app.get("/{site_name}/{path:path}")
async def serve_site_file(site_name: str, path: str = ""):
    """Serve static files from a deployed site."""
    site_dir = SITES_DIR / site_name

    if not site_dir.exists():
        raise HTTPException(status_code=404, detail="Site not found")

    if not path or path.endswith("/"):
        path = path.rstrip("/") + "/index.html" if path else "index.html"

    file_path = (site_dir / path).resolve()

    if not str(file_path).startswith(str(site_dir.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        html_path = file_path.with_suffix(".html")
        if html_path.exists():
            file_path = html_path
        else:
            raise HTTPException(status_code=404, detail="File not found")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not a file")

    return FileResponse(file_path)


@app.get("/{site_name}")
async def serve_site_root(site_name: str):
    """Redirect to site with trailing slash."""
    return FileResponse(SITES_DIR / site_name / "index.html")


# Need asyncio import for timeout
import asyncio
```

### Step 6: Create Brain Frontend

**File:** `services/wendy-sites/backend/static/brain/index.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wendy's Brain</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background: #0a0a0a;
            color: #e0e0e0;
            font-family: 'SF Mono', 'Consolas', 'Monaco', monospace;
            font-size: 14px;
            line-height: 1.5;
            min-height: 100vh;
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
        }

        /* Auth Screen */
        .auth-screen {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 20px;
        }

        .auth-screen h1 {
            font-size: 24px;
            font-weight: normal;
            margin-bottom: 8px;
        }

        .auth-screen p {
            color: #666;
            margin-bottom: 24px;
        }

        .auth-form {
            display: flex;
            gap: 8px;
        }

        .auth-form input {
            background: #111;
            border: 1px solid #333;
            border-radius: 6px;
            color: #fff;
            font-family: inherit;
            font-size: 14px;
            padding: 10px 14px;
            width: 200px;
        }

        .auth-form input:focus {
            outline: none;
            border-color: #555;
        }

        .auth-form button {
            background: #222;
            border: 1px solid #333;
            border-radius: 6px;
            color: #fff;
            cursor: pointer;
            font-family: inherit;
            font-size: 14px;
            padding: 10px 20px;
        }

        .auth-form button:hover {
            background: #333;
        }

        .auth-error {
            color: #f87171;
            margin-top: 12px;
            font-size: 13px;
        }

        /* Feed Screen */
        .feed-screen {
            display: none;
        }

        .feed-screen.active {
            display: block;
        }

        header {
            text-align: center;
            padding: 40px 0;
            border-bottom: 1px solid #222;
            margin-bottom: 20px;
        }

        header h1 {
            font-size: 24px;
            font-weight: normal;
            color: #fff;
        }

        header p {
            color: #666;
            margin-top: 8px;
        }

        .status {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            margin-top: 12px;
        }

        .status.connected { background: #1a3a1a; color: #4ade80; }
        .status.connecting { background: #3a3a1a; color: #facc15; }
        .status.disconnected { background: #3a1a1a; color: #f87171; }

        #feed {
            display: flex;
            flex-direction: column;
            gap: 8px;
            padding-bottom: 40px;
        }

        .event {
            padding: 12px 16px;
            border-radius: 8px;
            background: #111;
            border-left: 3px solid #333;
            animation: fadeIn 0.2s ease-out;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .event.text { border-left-color: #60a5fa; }
        .event.tool-use { border-left-color: #f59e0b; }
        .event.tool-result { border-left-color: #10b981; }
        .event.system { border-left-color: #8b5cf6; opacity: 0.7; }
        .event.error { border-left-color: #ef4444; }

        .event-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 8px;
            font-size: 12px;
            color: #666;
        }

        .event-type {
            text-transform: uppercase;
            font-weight: 600;
        }

        .event.text .event-type { color: #60a5fa; }
        .event.tool-use .event-type { color: #f59e0b; }
        .event.tool-result .event-type { color: #10b981; }
        .event.system .event-type { color: #8b5cf6; }
        .event.error .event-type { color: #ef4444; }

        .event-content {
            white-space: pre-wrap;
            word-break: break-word;
            font-size: 13px;
        }

        .tool-name {
            color: #fbbf24;
            font-weight: 600;
        }

        .idle-notice {
            text-align: center;
            color: #444;
            padding: 40px;
            font-style: italic;
        }
    </style>
</head>
<body>
    <!-- Auth Screen -->
    <div id="auth-screen" class="auth-screen">
        <h1>Wendy's Brain</h1>
        <p>Enter the code to watch Wendy think</p>
        <form class="auth-form" onsubmit="handleAuth(event)">
            <input type="text" id="code-input" placeholder="Code word" autocomplete="off" autofocus>
            <button type="submit">Enter</button>
        </form>
        <div id="auth-error" class="auth-error"></div>
    </div>

    <!-- Feed Screen -->
    <div id="feed-screen" class="feed-screen">
        <div class="container">
            <header>
                <h1>Wendy's Brain</h1>
                <p>Real-time view of Wendy's Claude Code session</p>
                <span id="status" class="status connecting">Connecting...</span>
            </header>

            <div id="feed">
                <div class="idle-notice">Waiting for activity...</div>
            </div>
        </div>
    </div>

    <script>
        const MAX_EVENTS = 100;
        let ws = null;

        // ==================== Auth ====================

        function getToken() {
            return localStorage.getItem('brain_token');
        }

        function setToken(token) {
            localStorage.setItem('brain_token', token);
        }

        function clearToken() {
            localStorage.removeItem('brain_token');
        }

        async function authenticate(code) {
            try {
                const res = await fetch('/api/brain/auth', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code })
                });

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'Invalid code');
                }

                const { token } = await res.json();
                setToken(token);
                return true;
            } catch (e) {
                throw e;
            }
        }

        async function handleAuth(e) {
            e.preventDefault();
            const input = document.getElementById('code-input');
            const error = document.getElementById('auth-error');
            const code = input.value.trim();

            if (!code) return;

            error.textContent = '';
            input.disabled = true;

            try {
                await authenticate(code);
                showFeed();
                connect();
            } catch (e) {
                error.textContent = e.message || 'Authentication failed';
                input.disabled = false;
                input.focus();
            }
        }

        // ==================== UI ====================

        function showAuth() {
            document.getElementById('auth-screen').style.display = 'flex';
            document.getElementById('feed-screen').classList.remove('active');
        }

        function showFeed() {
            document.getElementById('auth-screen').style.display = 'none';
            document.getElementById('feed-screen').classList.add('active');
        }

        function setStatus(state, text) {
            const el = document.getElementById('status');
            el.className = 'status ' + state;
            el.textContent = text || state.charAt(0).toUpperCase() + state.slice(1);
        }

        // ==================== Feed ====================

        function formatTime(ts) {
            return new Date(ts).toLocaleTimeString();
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function truncate(str, len = 500) {
            if (!str || str.length <= len) return str;
            return str.slice(0, len) + '...';
        }

        function renderEvent(data) {
            const { ts, event } = data;
            if (!event) return null;

            const div = document.createElement('div');
            div.className = 'event';

            let type = '';
            let content = '';

            if (event.type === 'ping') {
                return null; // Skip ping events
            } else if (event.type === 'system') {
                div.classList.add('system');
                type = event.subtype || 'system';
                content = event.subtype === 'init' ? 'Session started' : JSON.stringify(event);
            } else if (event.type === 'assistant') {
                const blocks = event.message?.content || [];
                for (const block of blocks) {
                    if (block.type === 'text' && block.text) {
                        div.classList.add('text');
                        type = 'thinking';
                        content = escapeHtml(block.text);
                    } else if (block.type === 'tool_use') {
                        div.classList.add('tool-use');
                        type = 'tool';
                        const input = typeof block.input === 'string'
                            ? block.input
                            : JSON.stringify(block.input, null, 2);
                        content = `<span class="tool-name">${escapeHtml(block.name)}</span>\n${escapeHtml(truncate(input, 300))}`;
                    }
                }
            } else if (event.type === 'user') {
                const blocks = event.message?.content || [];
                for (const block of blocks) {
                    if (block.type === 'tool_result') {
                        div.classList.add('tool-result');
                        type = 'result';
                        content = escapeHtml(truncate(block.content || '', 300));
                    }
                }
            } else if (event.type === 'result') {
                div.classList.add('system');
                type = 'complete';
                content = `Session complete (${event.num_turns || '?'} turns)`;
            }

            if (!type) return null;

            const header = document.createElement('div');
            header.className = 'event-header';

            const typeSpan = document.createElement('span');
            typeSpan.className = 'event-type';
            typeSpan.textContent = type;

            const timeSpan = document.createElement('span');
            timeSpan.className = 'event-time';
            timeSpan.textContent = formatTime(ts);

            header.appendChild(typeSpan);
            header.appendChild(timeSpan);

            const contentDiv = document.createElement('div');
            contentDiv.className = 'event-content';
            contentDiv.innerHTML = content; // Already escaped above

            div.appendChild(header);
            div.appendChild(contentDiv);

            return div;
        }

        function addEvent(data) {
            const feed = document.getElementById('feed');

            // Remove idle notice if present
            const idle = feed.querySelector('.idle-notice');
            if (idle) idle.remove();

            const el = renderEvent(data);
            if (!el) return;

            feed.appendChild(el);

            // Limit events
            while (feed.children.length > MAX_EVENTS) {
                feed.removeChild(feed.firstChild);
            }

            // Auto-scroll if near bottom
            const scrollBottom = window.innerHeight + window.scrollY;
            const docHeight = document.documentElement.scrollHeight;
            if (docHeight - scrollBottom < 200) {
                window.scrollTo(0, document.body.scrollHeight);
            }
        }

        // ==================== WebSocket ====================

        function connect() {
            const token = getToken();
            if (!token) {
                showAuth();
                return;
            }

            setStatus('connecting', 'Connecting...');

            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${location.host}/ws/brain?token=${encodeURIComponent(token)}`);

            ws.onopen = () => {
                setStatus('connected', 'Connected');
            };

            ws.onclose = (e) => {
                ws = null;

                if (e.code === 4001) {
                    // Token invalid/expired
                    clearToken();
                    showAuth();
                    document.getElementById('auth-error').textContent = 'Session expired, please re-enter code';
                } else if (e.code === 4002) {
                    setStatus('disconnected', 'Server full');
                } else {
                    setStatus('disconnected', 'Disconnected');
                    // Reconnect after delay
                    setTimeout(connect, 3000);
                }
            };

            ws.onerror = () => {
                setStatus('disconnected', 'Error');
            };

            ws.onmessage = (e) => {
                try {
                    const data = JSON.parse(e.data);
                    if (data.type === 'ping') {
                        // Respond to keepalive
                        ws.send('pong');
                    } else {
                        addEvent(data);
                    }
                } catch (err) {
                    console.error('Parse error:', err);
                }
            };
        }

        // ==================== Init ====================

        function init() {
            // Check for ?key= param
            const params = new URLSearchParams(location.search);
            const urlKey = params.get('key');

            if (urlKey) {
                // Try to authenticate with URL key
                authenticate(urlKey)
                    .then(() => {
                        // Clean URL
                        history.replaceState({}, '', '/');
                        showFeed();
                        connect();
                    })
                    .catch((e) => {
                        // Clean URL and show auth with error
                        history.replaceState({}, '', '/');
                        showAuth();
                        document.getElementById('auth-error').textContent = e.message || 'Invalid code';
                    });
            } else if (getToken()) {
                // Have existing token, try to connect
                showFeed();
                connect();
            } else {
                // No token, show auth
                showAuth();
            }
        }

        init();
    </script>
</body>
</html>
```

### Step 7: Update Deployment Files

**File:** `services/wendy-sites/deploy/.env.example`

```bash
# Site deployment token (for wendy-bot to deploy sites)
DEPLOY_TOKEN=generate-a-secure-token-here

# Brain feed access code (what users type to access the feed)
BRAIN_ACCESS_CODE=wendyiscool

# Brain feed token signing secret (generate with: python3 -c "import secrets; print(secrets.token_hex(32))")
BRAIN_SECRET=generate-a-secure-secret-here
```

**File:** `services/wendy-sites/deploy/docker-compose.yml`

```yaml
services:
  backend:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    ports:
      - "0.0.0.0:8910:8000"
    volumes:
      - sites_data:/data/sites
      # Mount wendy-bot's data directory (read-only) for brain feed
      # Note: wendy-bot uses named volume 'wendy_data'
      - /var/lib/docker/volumes/wendy_data/_data:/data/wendy:ro
    environment:
      - DEPLOY_TOKEN=${DEPLOY_TOKEN}
      - BRAIN_ACCESS_CODE=${BRAIN_ACCESS_CODE}
      - BRAIN_SECRET=${BRAIN_SECRET}
    restart: unless-stopped

volumes:
  sites_data:
```

**File:** `services/wendy-sites/deploy/Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY backend/ ./backend/

# Create static directory structure
RUN mkdir -p backend/static/brain

# Copy static files
COPY backend/static/ ./backend/static/

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Step 8: Create Package Structure

**File:** `services/wendy-sites/backend/__init__.py`

```python
"""Wendy Sites - Static HTML deployment and Brain Feed service."""
```

### Step 9: Update deploy.sh

**File:** `services/wendy-sites/deploy.sh`

```bash
#!/bin/bash
set -e

SERVICE_NAME="wendy-sites"
REMOTE_HOST="ubuntu@100.120.250.100"
REMOTE_DIR="/srv/$SERVICE_NAME"

echo "Deploying $SERVICE_NAME..."

# Create tarball
cd "$(dirname "$0")"
tar --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='node_modules' \
    -czf /tmp/$SERVICE_NAME.tar.gz \
    backend/ deploy/

# Upload
scp /tmp/$SERVICE_NAME.tar.gz $REMOTE_HOST:/tmp/

# Deploy
ssh $REMOTE_HOST << EOF
    set -e
    mkdir -p $REMOTE_DIR
    tar -xzf /tmp/$SERVICE_NAME.tar.gz -C $REMOTE_DIR
    cd $REMOTE_DIR/deploy

    # Create .env if it doesn't exist
    if [ ! -f .env ]; then
        cp .env.example .env
        echo "Created .env from example - please configure it!"
    fi

    # Build and restart
    docker compose -p $SERVICE_NAME up -d --build

    echo "Deployed successfully!"
EOF

rm /tmp/$SERVICE_NAME.tar.gz
echo "Done!"
```

## File Structure Summary

```
services/wendy-sites/
├── backend/
│   ├── __init__.py
│   ├── main.py              # FastAPI app with brain endpoints
│   ├── auth.py              # Token generation/verification
│   ├── brain.py             # WebSocket + file watching
│   ├── requirements.txt
│   └── static/
│       └── brain/
│           └── index.html   # Brain feed frontend
├── deploy/
│   ├── .env.example
│   ├── docker-compose.yml
│   └── Dockerfile
├── deploy.sh
└── BRAIN_FEED_PLAN.md
```

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `DEPLOY_TOKEN` | Token for site deployment API | `abc123...` |
| `BRAIN_ACCESS_CODE` | Code word users enter to access feed | `wendyiscool` |
| `BRAIN_SECRET` | Secret for signing access tokens | `def456...` |

## Testing

1. **Local testing:**
   ```bash
   cd services/wendy-sites/backend

   # Create mock stream file
   mkdir -p /tmp/wendy
   echo '{"ts":1704067200000,"event":{"type":"assistant","message":{"content":[{"type":"text","text":"Hello!"}]}}}' > /tmp/wendy/stream.jsonl

   # Run with test config
   BRAIN_ACCESS_CODE=test BRAIN_SECRET=secret123 \
   python -c "import sys; sys.path.insert(0,'.'); from main import app; import uvicorn; uvicorn.run(app, port=8000)"
   ```

2. **Test auth:**
   ```bash
   curl -X POST http://localhost:8000/api/brain/auth \
     -H "Content-Type: application/json" \
     -d '{"code": "test"}'
   ```

3. **Test WebSocket:**
   ```javascript
   const ws = new WebSocket('ws://localhost:8000/ws/brain?token=TOKEN_HERE');
   ws.onmessage = (e) => console.log(JSON.parse(e.data));
   ```

## Deployment Checklist

1. [ ] Generate secrets:
   ```bash
   python3 -c "import secrets; print('BRAIN_SECRET=' + secrets.token_hex(32))"
   ```

2. [ ] Configure `.env` on server with `BRAIN_ACCESS_CODE` and `BRAIN_SECRET`

3. [ ] Deploy wendy-sites:
   ```bash
   cd services/wendy-sites && ./deploy.sh
   ```

4. [ ] Verify health endpoint:
   ```bash
   curl https://wendy.monster/health
   ```

5. [ ] Test access:
   - Visit `https://wendy.monster/`
   - Enter code word
   - Or share `https://wendy.monster/?key=wendyiscool`

6. [ ] Verify WebSocket connects and receives events

## Security Summary

- Code word validated server-side only
- Tokens are HMAC-signed with expiry
- WebSocket validates token before streaming
- File access is read-only
- Max 100 concurrent WebSocket connections
- Ping/pong keepalive prevents zombie connections
