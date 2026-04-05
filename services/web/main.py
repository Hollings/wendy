"""Wendy Web - Unified static sites + game servers + brain feed.

Merges wendy-sites and wendy-games into a single service.

Endpoints:
    Sites:
        POST /api/sites/deploy  - Deploy static site (tarball)
        GET  /api/sites         - List deployed sites
        DEL  /api/sites/{name}  - Delete site

    Games:
        POST /api/games/deploy        - Deploy game (tarball -> Docker container)
        GET  /api/games               - List deployed games
        GET  /api/games/{name}        - Get game status
        POST /api/games/{name}/restart
        DEL  /api/games/{name}
        GET  /api/games/{name}/logs

    Game proxying:
        WS   /game/{name}/ws          - WebSocket proxy to game container
        *    /game/{name}/{path:path} - HTTP proxy to game container

    Brain feed:
        GET  /                         - Dashboard
        POST /api/brain/auth
        WS   /ws/brain
        GET  /api/brain/stats
        GET  /api/brain/usage
        GET  /api/brain/agents
        GET  /api/brain/agents/{id}
        GET  /api/brain/beads
        GET  /api/brain/beads/{id}/log

    Webhooks:
        POST /webhook/{token}
        GET  /webhook/{token}/test

    Static site serving (catch-all, last):
        GET  /{site_name}/{path:path}
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import auth
import brain
import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

app = FastAPI(title="Wendy Web", version="1.0.0")

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
STATIC_DIR: Path = Path(__file__).parent / "static"

# Serve React brain-ui build assets (/assets/...) before any catch-all routes.
# Must be registered after STATIC_DIR is defined.
_brain_assets = STATIC_DIR / "brain" / "assets"
if _brain_assets.is_dir():
    app.mount("/assets", StaticFiles(directory=_brain_assets), name="brain-assets")

# Sites
SITES_DIR: Path = Path(os.environ.get("SITES_DIR", "/data/sites"))
MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024
BASE_URL: str = os.environ.get("BASE_URL", "https://wendy.monster")
SITE_NAME_RE: re.Pattern = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$|^[a-z0-9]$")
RESERVED_NAMES = {"api", "health", "admin", "static", "assets", "ws", "brain", "game", "webhook", "avatar"}

# Games
GAMES_TOKEN: str = os.environ.get("GAMES_TOKEN", DEPLOY_TOKEN)
GAMES_DIR: Path = Path(os.environ.get("GAMES_DIR", "/data/games"))
HOST_GAMES_DIR: str = os.environ.get("HOST_GAMES_DIR", str(GAMES_DIR))
RUNTIME_DIR: Path = Path(os.environ.get("RUNTIME_DIR", "/app/runtime"))
BASE_PORT: int = int(os.environ.get("BASE_PORT", "8921"))
MAX_GAMES: int = int(os.environ.get("MAX_GAMES", "20"))
DOCKER_NETWORK: str = os.environ.get("DOCKER_NETWORK", "wendy_default")
PORTS_FILE: Path = GAMES_DIR / "ports.json"

# Wendy integration
WENDY_DATA_DIR: Path = Path("/data/wendy")
WENDY_DB_PATH: Path = Path(os.getenv("WENDY_DB_PATH", "/data/wendy/shared/wendy.db"))
WEBHOOKS_FILE: Path = WENDY_DATA_DIR / "secrets" / "webhooks.json"
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_MAX_PAYLOAD: int = 1024 * 1024
WEBHOOK_RATE_LIMIT: int = 10
USAGE_DATA_FILE: Path = Path("/data/wendy/usage_data.json")

_webhook_rate_limits: dict[str, list[float]] = {}
_ports_lock = asyncio.Lock()

SITES_DIR.mkdir(parents=True, exist_ok=True)
GAMES_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Startup
# =============================================================================


@app.on_event("startup")
async def startup() -> None:
    if auth.is_configured():
        brain.start_watcher()


# =============================================================================
# Shared helpers
# =============================================================================


def _verify_token(authorization: str | None, token: str) -> None:
    if not token:
        raise HTTPException(status_code=500, detail="Server not configured")
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization required")
    t = authorization.removeprefix("Bearer ")
    if t != token:
        raise HTTPException(status_code=403, detail="Invalid token")


def _valid_name(name: str) -> bool:
    return bool(name) and bool(SITE_NAME_RE.match(name))


def _safe_extract(tar_path: Path, dest_dir: Path) -> None:
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            if Path(member.name).is_absolute():
                raise HTTPException(status_code=400, detail="Tarball contains absolute paths")
            if not str((dest_dir / member.name).resolve()).startswith(str(dest_dir.resolve())):
                raise HTTPException(status_code=400, detail="Tarball contains path traversal")
            if member.name.startswith(".") and member.name != ".":
                continue
        tar.extractall(dest_dir, filter="data")


# =============================================================================
# Static sites
# =============================================================================


@app.post("/api/sites/deploy")
async def deploy_site(
    name: str = Form(...),
    files: UploadFile = File(...),
    authorization: str | None = Header(None),
) -> JSONResponse:
    _verify_token(authorization, DEPLOY_TOKEN)
    if not _valid_name(name):
        raise HTTPException(status_code=400, detail="Invalid site name")
    if name in RESERVED_NAMES:
        raise HTTPException(status_code=400, detail=f"Name '{name}' is reserved")

    content = await files.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Upload too large (max 50MB)")

    tmp_tar = Path(f"/tmp/site_{name}.tar.gz")
    try:
        tmp_tar.write_bytes(content)
        if not tarfile.is_tarfile(tmp_tar):
            raise HTTPException(status_code=400, detail="Not a valid tarball")

        site_dir = SITES_DIR / name
        if site_dir.exists():
            shutil.rmtree(site_dir)
        site_dir.mkdir(parents=True)

        _safe_extract(tmp_tar, site_dir)

        if not (site_dir / "index.html").exists() and not (site_dir / "index.htm").exists():
            shutil.rmtree(site_dir)
            raise HTTPException(status_code=400, detail="Site must contain index.html")

        url = f"{BASE_URL}/{name}/"
        return JSONResponse({"success": True, "url": url, "message": f"Site deployed at {url}"})
    finally:
        tmp_tar.unlink(missing_ok=True)


@app.get("/api/sites")
async def list_sites(authorization: str | None = Header(None)) -> dict:
    _verify_token(authorization, DEPLOY_TOKEN)
    sites = [
        {"name": d.name, "url": f"{BASE_URL}/{d.name}/"}
        for d in SITES_DIR.iterdir() if d.is_dir()
    ]
    return {"sites": sites}


@app.delete("/api/sites/{name}")
async def delete_site(name: str, authorization: str | None = Header(None)) -> dict:
    _verify_token(authorization, DEPLOY_TOKEN)
    site_dir = SITES_DIR / name
    if not site_dir.exists():
        raise HTTPException(status_code=404, detail="Site not found")
    shutil.rmtree(site_dir)
    return {"success": True}


# =============================================================================
# Games
# =============================================================================


def _load_ports() -> dict[str, int]:
    if PORTS_FILE.exists():
        return json.loads(PORTS_FILE.read_text())
    return {}


def _save_ports(ports: dict[str, int]) -> None:
    PORTS_FILE.write_text(json.dumps(ports, indent=2))


async def _allocate_port(game_name: str) -> int:
    async with _ports_lock:
        ports = _load_ports()
        if game_name in ports:
            return ports[game_name]
        used = set(ports.values())
        for port in range(BASE_PORT, BASE_PORT + MAX_GAMES):
            if port not in used:
                ports[game_name] = port
                _save_ports(ports)
                return port
    raise HTTPException(status_code=503, detail=f"No available ports (max {MAX_GAMES} games)")


def _container_name(game_name: str) -> str:
    return f"wendy-game-{game_name}"


def _docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker"] + args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Docker error: {result.stderr}")
    return result


def _is_running(game_name: str) -> bool:
    result = _docker(["ps", "-q", "-f", f"name={_container_name(game_name)}"], check=False)
    return bool(result.stdout.strip())


@app.post("/api/games/deploy")
async def deploy_game(
    name: str = Form(...),
    files: UploadFile = File(...),
    authorization: str | None = Header(None),
) -> JSONResponse:
    _verify_token(authorization, GAMES_TOKEN)
    if not _valid_name(name):
        raise HTTPException(status_code=400, detail="Invalid game name")

    content = await files.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Upload too large (max 10MB)")

    game_dir = GAMES_DIR / name
    tmp_tar = Path(f"/tmp/game_{name}.tar.gz")
    try:
        tmp_tar.write_bytes(content)
        if not tarfile.is_tarfile(tmp_tar):
            raise HTTPException(status_code=400, detail="Not a valid tarball")

        state_backup = None
        state_file = game_dir / "state.json"
        if state_file.exists():
            state_backup = state_file.read_text()

        if game_dir.exists():
            shutil.rmtree(game_dir)
        game_dir.mkdir(parents=True)

        _safe_extract(tmp_tar, game_dir)

        if state_backup:
            state_file.write_text(state_backup)
        elif not state_file.exists():
            state_file.write_text("{}")

        os.chown(state_file, 1993, 1993)

        if not (game_dir / "server.ts").exists():
            shutil.rmtree(game_dir)
            raise HTTPException(status_code=400, detail="server.ts not found")

        port = await _allocate_port(name)
        cname = _container_name(name)
        _docker(["stop", cname], check=False)
        _docker(["rm", cname], check=False)

        host_game_dir = f"{HOST_GAMES_DIR}/{name}"
        _docker([
            "run", "-d",
            "--name", cname,
            "--restart", "unless-stopped",
            "--network", DOCKER_NETWORK,
            "-p", f"0.0.0.0:{port}:8000",
            "-v", f"{host_game_dir}:/app/game:ro",
            "-v", f"{host_game_dir}/state.json:/data/state.json",
            "-e", "PORT=8000",
            "-e", "STATE_FILE=/data/state.json",
            "--memory", "256m",
            "--cpus", "0.5",
            "wendy-games-runtime",
        ])

        return JSONResponse({
            "success": True,
            "url": f"{BASE_URL}/game/{name}/",
            "ws": f"wss://{BASE_URL.split('://', 1)[-1]}/game/{name}/ws",
            "port": port,
            "message": f"Game '{name}' deployed",
        })
    finally:
        tmp_tar.unlink(missing_ok=True)


@app.get("/api/games")
async def list_games(authorization: str | None = Header(None)) -> dict:
    _verify_token(authorization, GAMES_TOKEN)
    ports = _load_ports()
    return {"games": [
        {"name": n, "port": p, "url": f"{BASE_URL}/game/{n}/", "running": _is_running(n)}
        for n, p in ports.items()
    ]}


@app.get("/api/games/{name}")
async def get_game(name: str, authorization: str | None = Header(None)) -> dict:
    _verify_token(authorization, GAMES_TOKEN)
    ports = _load_ports()
    if name not in ports:
        raise HTTPException(status_code=404, detail="Game not found")
    return {"name": name, "port": ports[name], "url": f"{BASE_URL}/game/{name}/", "running": _is_running(name)}


@app.post("/api/games/{name}/restart")
async def restart_game(name: str, authorization: str | None = Header(None)) -> dict:
    _verify_token(authorization, GAMES_TOKEN)
    if name not in _load_ports():
        raise HTTPException(status_code=404, detail="Game not found")
    _docker(["restart", _container_name(name)])
    return {"success": True}


@app.delete("/api/games/{name}")
async def delete_game(name: str, authorization: str | None = Header(None)) -> dict:
    _verify_token(authorization, GAMES_TOKEN)
    ports = _load_ports()
    if name not in ports:
        raise HTTPException(status_code=404, detail="Game not found")
    cname = _container_name(name)
    _docker(["stop", cname], check=False)
    _docker(["rm", cname], check=False)
    game_dir = GAMES_DIR / name
    if game_dir.exists():
        shutil.rmtree(game_dir)
    del ports[name]
    _save_ports(ports)
    return {"success": True}


@app.get("/api/games/{name}/logs")
async def game_logs(name: str, lines: int = 50, authorization: str | None = Header(None)) -> dict:
    _verify_token(authorization, GAMES_TOKEN)
    if name not in _load_ports():
        raise HTTPException(status_code=404, detail="Game not found")
    result = _docker(["logs", "--tail", str(lines), _container_name(name)], check=False)
    return {"name": name, "logs": result.stdout + result.stderr}


# =============================================================================
# Game proxying
# =============================================================================


@app.websocket("/game/{name}/ws")
async def proxy_websocket(websocket: WebSocket, name: str) -> None:
    import websockets as ws_lib

    if name not in _load_ports():
        await websocket.close(code=4004, reason="Game not found")
        return

    cname = _container_name(name)
    await websocket.accept()

    try:
        async with ws_lib.connect(f"ws://{cname}:8000") as backend:
            async def fwd_to_backend():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await backend.send(data)
                except WebSocketDisconnect:
                    pass

            async def fwd_to_client():
                try:
                    async for msg in backend:
                        await websocket.send_text(msg)
                except Exception:
                    pass

            await asyncio.gather(fwd_to_backend(), fwd_to_client(), return_exceptions=True)
    except Exception as e:
        try:
            await websocket.close(code=1011, reason=str(e)[:100])
        except Exception:
            pass


def _strip_proxy_headers(headers: dict) -> dict:
    skip = {"content-encoding", "content-length", "transfer-encoding"}
    return {k: v for k, v in headers.items() if k.lower() not in skip}


@app.get("/game/{name}/")
async def proxy_game_root(name: str) -> Response:
    if name not in _load_ports():
        raise HTTPException(status_code=404, detail="Game not found")
    cname = _container_name(name)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"http://{cname}:8000/")
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=_strip_proxy_headers(dict(resp.headers)))


@app.api_route("/game/{name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_game(name: str, path: str, request: Request) -> Response:
    if name not in _load_ports():
        raise HTTPException(status_code=404, detail="Game not found")
    cname = _container_name(name)
    url = f"http://{cname}:8000/{path}"
    if request.query_params:
        url += f"?{request.query_params}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "accept-encoding"}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(request.method, url, headers=headers, content=await request.body())
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=_strip_proxy_headers(dict(resp.headers)))


# =============================================================================
# Brain feed
# =============================================================================


class BrainAuthRequest(BaseModel):
    code: str


class BrainAuthResponse(BaseModel):
    token: str


async def _require_brain_auth(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
) -> None:
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")
    auth_token = None
    if authorization and authorization.startswith("Bearer "):
        auth_token = authorization[7:]
    elif token:
        auth_token = token
    if not auth_token or not auth.verify_token(auth_token):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


@app.get("/", response_class=HTMLResponse)
async def serve_brain_page() -> HTMLResponse:
    brain_html = STATIC_DIR / "brain" / "index.html"
    if brain_html.exists():
        return HTMLResponse(brain_html.read_text(), headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>Brain feed not configured</h1>", status_code=503)


@app.post("/api/brain/auth", response_model=BrainAuthResponse)
async def brain_authenticate(request: BrainAuthRequest) -> BrainAuthResponse:
    if not auth.is_configured():
        raise HTTPException(status_code=503, detail="Brain feed not configured")
    if not auth.verify_code(request.code):
        raise HTTPException(status_code=401, detail="Invalid code")
    return BrainAuthResponse(token=auth.generate_token())


@app.websocket("/ws/brain")
async def brain_websocket(websocket: WebSocket, token: str = Query("")) -> None:
    if not auth.verify_token(token):
        await websocket.accept()
        await websocket.close(code=4001, reason="Invalid or expired token")
        return
    if not await brain.add_client(websocket):
        await websocket.close(code=4002, reason="Server at capacity")
        return
    await websocket.accept()
    try:
        # Send channel names so the UI can label channel chips immediately
        channels_map = brain.get_channels_map()
        await websocket.send_text(json.dumps({"type": "channels_map", "channels": channels_map}))

        for event in brain.get_recent_events():
            await websocket.send_text(event)
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except TimeoutError:
                await websocket.send_text('{"type":"ping"}')
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        brain.remove_client(websocket)


@app.get("/api/brain/stats")
async def brain_stats(_auth: None = Depends(_require_brain_auth)) -> dict:
    return brain.get_stats()


@app.get("/api/brain/channels")
async def brain_channels(_auth: None = Depends(_require_brain_auth)) -> dict:
    """Return {channel_id: folder_name} mapping for channel chip labels."""
    return {"channels": brain.get_channels_map()}


@app.get("/api/brain/usage")
async def brain_usage(_auth: None = Depends(_require_brain_auth)) -> dict:
    if not USAGE_DATA_FILE.exists():
        return {"available": False, "message": "Usage data not available yet"}
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
        return {"available": False, "message": f"Error reading usage data: {e}"}


@app.get("/api/brain/agents")
async def brain_agents(_auth: None = Depends(_require_brain_auth)) -> dict:
    return {"agents": brain.list_agents()}


@app.get("/api/brain/agents/{agent_id}")
async def brain_agent_events(
    agent_id: str, limit: int = 50, _auth: None = Depends(_require_brain_auth),
) -> dict:
    return {"agent_id": agent_id, "events": brain.get_agent_events(agent_id, limit)}


@app.get("/api/brain/beads")
async def brain_beads(_auth: None = Depends(_require_brain_auth)) -> dict:
    jsonl_path = brain.BEADS_JSONL
    if not jsonl_path.exists():
        return {"beads": []}

    issues_by_id: dict = {}
    for line in jsonl_path.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if issue_id := data.get("id"):
                issues_by_id[issue_id] = data
        except json.JSONDecodeError:
            continue

    beads = [
        {
            "id": iid,
            "title": d.get("title", "Untitled"),
            "status": d.get("status", "open"),
            "priority": d.get("priority", 2),
            "created": d.get("created"),
            "updated": d.get("updated", d.get("created")),
            "labels": d.get("labels", []),
        }
        for iid, d in issues_by_id.items()
    ]

    status_order = {"in_progress": 0, "open": 1, "closed": 2, "tombstone": 3}
    active = [b for b in beads if b["status"] in ("in_progress", "open")]
    closed = sorted([b for b in beads if b["status"] == "closed"],
                    key=lambda b: b.get("updated") or "", reverse=True)
    tombstone = sorted([b for b in beads if b["status"] == "tombstone"],
                       key=lambda b: b.get("updated") or "", reverse=True)
    active.sort(key=lambda b: (status_order.get(b["status"], 4), b.get("priority", 2)))

    return {"beads": active + closed[:10] + tombstone[:5]}


@app.get("/api/brain/beads/{task_id}/log")
async def brain_task_log(
    task_id: str, offset: int = 0, _auth: None = Depends(_require_brain_auth),
) -> dict:
    logs_dir = Path("/data/wendy/orchestrator_logs")
    if not logs_dir.exists():
        return {"task_id": task_id, "log": "", "offset": 0, "complete": False}
    log_files = list(logs_dir.glob(f"agent_{task_id}_*.log"))
    if not log_files:
        return {"task_id": task_id, "log": "", "offset": 0, "complete": False}
    log_file = max(log_files, key=lambda f: f.stat().st_mtime)
    try:
        content = log_file.read_text()
        new_content = content[offset:] if offset < len(content) else ""
        complete = "=== TASK COMPLETE ===" in content or "=== TASK FAILED ===" in content
        return {"task_id": task_id, "log": new_content, "offset": len(content), "complete": complete}
    except OSError:
        return {"task_id": task_id, "log": "", "offset": 0, "complete": False}


# =============================================================================
# Webhooks
# =============================================================================


def _load_webhooks() -> dict:
    if not WEBHOOKS_FILE.exists():
        return {}
    try:
        return json.loads(WEBHOOKS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _validate_webhook_token(token: str) -> dict | None:
    for name, config in _load_webhooks().items():
        if config.get("token") == token:
            return {"name": name, **config}
    return None


def _check_rate_limit(token: str) -> bool:
    now = time.time()
    minute_ago = now - 60
    _webhook_rate_limits.setdefault(token, [])
    _webhook_rate_limits[token] = [ts for ts in _webhook_rate_limits[token] if ts > minute_ago]
    if len(_webhook_rate_limits[token]) >= WEBHOOK_RATE_LIMIT:
        return False
    _webhook_rate_limits[token].append(now)
    return True


def _detect_source(headers: dict) -> tuple[str, str]:
    if "x-github-event" in headers:
        return "github", headers["x-github-event"]
    if "x-gitlab-event" in headers:
        return "gitlab", headers["x-gitlab-event"]
    if "x-event-key" in headers:
        return "bitbucket", headers["x-event-key"]
    return "webhook", "unknown"


def _format_github(event_type: str, payload: dict) -> str:
    repo = payload.get("repository", {}).get("full_name", "unknown")
    sender = payload.get("sender", {}).get("login", "someone")
    if event_type == "push":
        commits = payload.get("commits", [])
        branch = payload.get("ref", "").replace("refs/heads/", "")
        if len(commits) == 1:
            msg = commits[0].get("message", "").split("\n")[0][:50]
            return f'{sender} pushed to {branch} in {repo}: "{msg}"'
        return f"{sender} pushed {len(commits)} commits to {branch} in {repo}"
    if event_type == "pull_request":
        pr = payload.get("pull_request", {})
        return f'{sender} {payload.get("action", "updated")} PR #{pr.get("number")} in {repo}: "{pr.get("title", "")[:50]}"'
    if event_type == "issues":
        issue = payload.get("issue", {})
        return f'{sender} {payload.get("action", "updated")} issue #{issue.get("number")} in {repo}: "{issue.get("title", "")[:50]}"'
    if event_type == "ping":
        return f"GitHub ping from {repo} - webhook configured successfully"
    return f"GitHub {event_type} event from {sender} in {repo}"


def _format_summary(source: str, event_type: str, payload: dict) -> str:
    if source == "github":
        return _format_github(event_type, payload)
    return f"Webhook event: {event_type}"


def _write_notification(channel_id: str, source: str, event_type: str, summary: str, payload: dict) -> None:
    try:
        channel_id_int = int(channel_id)
    except ValueError:
        return
    payload_str = json.dumps({"event_type": event_type, "raw": payload})
    try:
        WENDY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(WENDY_DB_PATH, timeout=30.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL, source TEXT NOT NULL, channel_id INTEGER,
                    title TEXT NOT NULL, payload TEXT,
                    seen_by_wendy INTEGER DEFAULT 0, seen_by_proxy INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "INSERT INTO notifications (type, source, channel_id, title, payload) VALUES (?,?,?,?,?)",
                ("webhook", source, channel_id_int, summary, payload_str),
            )
            conn.execute("""
                DELETE FROM notifications WHERE id NOT IN (
                    SELECT id FROM notifications ORDER BY created_at DESC LIMIT 100
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"Failed to write webhook notification: {e}")


@app.post("/webhook/{token}")
async def receive_webhook(token: str, request: Request) -> JSONResponse:
    config = _validate_webhook_token(token)
    if not config:
        raise HTTPException(status_code=404, detail="Not found")
    if not _check_rate_limit(token):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    body = await request.body()

    if WEBHOOK_SECRET:
        sig = request.headers.get("x-hub-signature-256")
        if not sig:
            raise HTTPException(status_code=401, detail="Missing signature")
        expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=403, detail="Invalid signature")

    if len(body) > WEBHOOK_MAX_PAYLOAD:
        raise HTTPException(status_code=413, detail="Payload too large")

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"raw": body.decode("utf-8", errors="replace")}

    headers = {k.lower(): v for k, v in request.headers.items()}
    source, event_type = _detect_source(headers)
    summary = _format_summary(source, event_type, payload)
    _write_notification(config["channel_id"], source, event_type, summary, payload)

    return JSONResponse({"success": True, "message": "Webhook received", "event_id": str(uuid.uuid4())})


@app.get("/webhook/{token}/test")
async def test_webhook(token: str) -> JSONResponse:
    config = _validate_webhook_token(token)
    if not config:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse({"valid": True, "channel": config["name"]})


# =============================================================================
# Avatar static files
# =============================================================================

AVATAR_DIR: Path = STATIC_DIR / "avatar"


@app.get("/avatar/")
async def serve_avatar_root() -> FileResponse:
    return FileResponse(AVATAR_DIR / "index.html")


@app.get("/avatar/{path:path}")
async def serve_avatar(path: str) -> FileResponse:
    file_path = (AVATAR_DIR / path).resolve()
    if not str(file_path).startswith(str(AVATAR_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media_type = None
    if path.endswith(".js"):
        media_type = "application/javascript"
    elif path.endswith(".css"):
        media_type = "text/css"
    return FileResponse(file_path, media_type=media_type)


# =============================================================================
# Health
# =============================================================================


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "sites_count": sum(1 for d in SITES_DIR.iterdir() if d.is_dir()) if SITES_DIR.exists() else 0,
        "games_count": len(_load_ports()),
        "brain_clients": brain.client_count(),
        "brain_configured": auth.is_configured(),
    }


# =============================================================================
# Static site serving (catch-all -- must be last)
# =============================================================================


@app.get("/{site_name}")
async def serve_site_root(site_name: str) -> FileResponse:
    site_dir = SITES_DIR / site_name
    if not site_dir.exists():
        raise HTTPException(status_code=404, detail="Site not found")
    return FileResponse(site_dir / "index.html")


@app.get("/{site_name}/{path:path}")
async def serve_site(site_name: str, path: str = "") -> FileResponse:
    site_dir = SITES_DIR / site_name
    if not site_dir.exists():
        raise HTTPException(status_code=404, detail="Site not found")

    file_path = (site_dir / (path or "index.html")).resolve()
    if not str(file_path).startswith(str(site_dir.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        html_path = file_path.with_suffix(".html")
        if html_path.exists():
            file_path = html_path
        else:
            raise HTTPException(status_code=404, detail="Not found")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(file_path)
