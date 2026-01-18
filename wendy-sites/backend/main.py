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
import os
import re
import shutil
import tarfile
from pathlib import Path

import auth
import brain
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

app = FastAPI(title="Wendy Sites", version="2.0.0")

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
                    "labels": data.get("labels", []),
                })
        except OSError:
            pass

    # Sort: in_progress first, then open, then closed
    status_order = {"in_progress": 0, "open": 1, "closed": 2}
    beads.sort(key=lambda b: (
        status_order.get(b["status"], 3),
        b.get("priority", 2) if b["status"] != "closed" else 999,
    ))

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
