"""
Wendy Sites - Static HTML deployment service for wendy.monster

Also serves the Brain Feed - real-time visualization of Wendy's Claude Code session.
"""

import asyncio
import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Header, UploadFile, WebSocket, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

import auth
import brain

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
    """Serve the brain feed page at root."""
    brain_html = STATIC_DIR / "brain" / "index.html"
    if brain_html.exists():
        return HTMLResponse(brain_html.read_text())
    return HTMLResponse("<h1>Brain feed not configured</h1>", status_code=503)


@app.get("/api/brain/stats")
async def brain_stats():
    """Get brain feed stats."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")
    return brain.get_stats()


@app.get("/api/brain/agents")
async def brain_agents():
    """List all subagents."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")
    return {"agents": brain.list_agents()}


@app.get("/api/brain/agents/{agent_id}")
async def brain_agent_events(agent_id: str, limit: int = 50):
    """Get events from a specific agent."""
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")
    events = brain.get_agent_events(agent_id, limit)
    return {"agent_id": agent_id, "events": events}


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
    except Exception:
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


# ==================== Site Deployment ====================

def verify_deploy_token(authorization: Optional[str]) -> None:
    """Verify the deploy token."""
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
    """Validate site name format."""
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
    """Safely extract tarball with path traversal protection."""
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


@app.post("/api/deploy")
async def deploy_site(
    name: str = Form(...),
    files: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Deploy a site from a tarball."""
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
async def list_sites(authorization: Optional[str] = Header(None)):
    """List all deployed sites."""
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
async def delete_site(name: str, authorization: Optional[str] = Header(None)):
    """Delete a deployed site."""
    verify_deploy_token(authorization)
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
async def serve_site_root(site_name: str):
    """Redirect to site with trailing slash."""
    return FileResponse(SITES_DIR / site_name / "index.html")
