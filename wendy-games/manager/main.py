"""
Wendy Games Manager - Manages game server deployments

This service:
- Handles game deployment requests
- Manages Docker containers for each game
- Tracks port allocations
- Provides game listing and status
"""

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

app = FastAPI(title="Wendy Games Manager", version="1.0.0")

# Configuration
DEPLOY_TOKEN = os.environ.get("DEPLOY_TOKEN", "")
GAMES_DIR = Path(os.environ.get("GAMES_DIR", "/data/games"))
# HOST_GAMES_DIR is the path Docker sees on the HOST for mounting volumes
# This is needed because the manager runs in a container but spawns sibling containers via docker.sock
HOST_GAMES_DIR = os.environ.get("HOST_GAMES_DIR", str(GAMES_DIR))
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR", "/app/runtime"))
BASE_PORT = int(os.environ.get("BASE_PORT", "8920"))
MAX_GAMES = int(os.environ.get("MAX_GAMES", "20"))
BASE_URL = os.environ.get("BASE_URL", "https://wendy.monster")
NETWORK_NAME = os.environ.get("DOCKER_NETWORK", "wendy-games_default")

# Port allocation file
PORTS_FILE = GAMES_DIR / "ports.json"

# Ensure directories exist
GAMES_DIR.mkdir(parents=True, exist_ok=True)


def load_ports() -> dict[str, int]:
    """Load port allocations."""
    if PORTS_FILE.exists():
        return json.loads(PORTS_FILE.read_text())
    return {}


def save_ports(ports: dict[str, int]) -> None:
    """Save port allocations."""
    PORTS_FILE.write_text(json.dumps(ports, indent=2))


def allocate_port(game_name: str) -> int:
    """Allocate a port for a game."""
    ports = load_ports()

    # Return existing port if already allocated
    if game_name in ports:
        return ports[game_name]

    # Find next available port
    used_ports = set(ports.values())
    for port in range(BASE_PORT, BASE_PORT + MAX_GAMES):
        if port not in used_ports:
            ports[game_name] = port
            save_ports(ports)
            return port

    raise HTTPException(status_code=503, detail="No available ports")


def verify_token(authorization: Optional[str]) -> None:
    """Verify the deploy token."""
    if not DEPLOY_TOKEN:
        raise HTTPException(status_code=500, detail="Server not configured")

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization required")

    token = authorization.replace("Bearer ", "")
    if token != DEPLOY_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


def validate_game_name(name: str) -> None:
    """Validate game name."""
    import re
    if not name or not re.match(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$|^[a-z0-9]$", name):
        raise HTTPException(
            status_code=400,
            detail="Game name must be lowercase alphanumeric with hyphens"
        )


def container_name(game_name: str) -> str:
    """Get Docker container name for a game."""
    return f"wendy-game-{game_name}"


def run_docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a Docker command."""
    result = subprocess.run(
        ["docker"] + args,
        capture_output=True,
        text=True
    )
    if check and result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Docker error: {result.stderr}"
        )
    return result


def is_game_running(game_name: str) -> bool:
    """Check if a game container is running."""
    result = run_docker(
        ["ps", "-q", "-f", f"name={container_name(game_name)}"],
        check=False
    )
    return bool(result.stdout.strip())


@app.get("/health")
async def health():
    """Health check."""
    ports = load_ports()
    return {
        "status": "healthy",
        "games_count": len(ports)
    }


@app.post("/api/deploy")
async def deploy_game(
    name: str = Form(...),
    files: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Deploy a game server."""
    verify_token(authorization)
    validate_game_name(name)

    # Read uploaded tarball
    content = await files.read()
    if len(content) > 10 * 1024 * 1024:  # 10 MB limit for game code
        raise HTTPException(status_code=413, detail="Upload too large (max 10MB)")

    game_dir = GAMES_DIR / name
    tmp_tar = Path(f"/tmp/game_{name}.tar.gz")

    try:
        # Save and extract tarball
        tmp_tar.write_bytes(content)

        if not tarfile.is_tarfile(tmp_tar):
            raise HTTPException(status_code=400, detail="Invalid tarball")

        # Clean existing game directory (but preserve state)
        state_backup = None
        state_file = game_dir / "state.json"
        if state_file.exists():
            state_backup = state_file.read_text()

        if game_dir.exists():
            shutil.rmtree(game_dir)
        game_dir.mkdir(parents=True)

        # Extract
        with tarfile.open(tmp_tar, "r:gz") as tar:
            tar.extractall(game_dir, filter="data")

        # Restore state if it existed
        if state_backup:
            state_file.write_text(state_backup)
        elif not state_file.exists():
            state_file.write_text("{}")

        # Make state.json writable by deno user (UID 1993)
        os.chown(state_file, 1993, 1993)

        # Verify server.ts exists
        server_file = game_dir / "server.ts"
        if not server_file.exists():
            shutil.rmtree(game_dir)
            raise HTTPException(status_code=400, detail="server.ts not found")

        # Allocate port
        port = allocate_port(name)

        # Stop existing container if running
        cname = container_name(name)
        run_docker(["stop", cname], check=False)
        run_docker(["rm", cname], check=False)

        # Start new container
        # Use HOST_GAMES_DIR for volume mounts since Docker runs on the host
        # Mount entire game directory so public/, assets/, etc. are available
        host_game_dir = f"{HOST_GAMES_DIR}/{name}"
        run_docker([
            "run", "-d",
            "--name", cname,
            "--restart", "unless-stopped",
            "--network", NETWORK_NAME,
            "-p", f"0.0.0.0:{port}:8000",
            "-v", f"{host_game_dir}:/app/game:ro",
            "-v", f"{host_game_dir}/state.json:/data/state.json",
            "-e", "PORT=8000",
            "-e", "STATE_FILE=/data/state.json",
            "--memory", "256m",
            "--cpus", "0.5",
            "wendy-games-runtime"
        ])

        return JSONResponse({
            "success": True,
            "url": f"{BASE_URL}/game/{name}/",
            "ws": f"wss://wendy.monster/game/{name}/ws",
            "port": port,
            "message": f"Game '{name}' deployed successfully"
        })

    finally:
        if tmp_tar.exists():
            tmp_tar.unlink()


@app.get("/api/games")
async def list_games(authorization: Optional[str] = Header(None)):
    """List all deployed games."""
    verify_token(authorization)

    ports = load_ports()
    games = []

    for name, port in ports.items():
        running = is_game_running(name)
        games.append({
            "name": name,
            "port": port,
            "url": f"{BASE_URL}/game/{name}/",
            "ws": f"wss://wendy.monster/game/{name}/ws",
            "running": running
        })

    return {"games": games}


@app.get("/api/games/{name}")
async def get_game(name: str, authorization: Optional[str] = Header(None)):
    """Get game status."""
    verify_token(authorization)
    validate_game_name(name)

    ports = load_ports()
    if name not in ports:
        raise HTTPException(status_code=404, detail="Game not found")

    port = ports[name]
    running = is_game_running(name)

    return {
        "name": name,
        "port": port,
        "url": f"{BASE_URL}/game/{name}/",
        "ws": f"wss://wendy.monster/game/{name}/ws",
        "running": running
    }


@app.post("/api/games/{name}/restart")
async def restart_game(name: str, authorization: Optional[str] = Header(None)):
    """Restart a game server."""
    verify_token(authorization)
    validate_game_name(name)

    ports = load_ports()
    if name not in ports:
        raise HTTPException(status_code=404, detail="Game not found")

    cname = container_name(name)
    run_docker(["restart", cname])

    return {"success": True, "message": f"Game '{name}' restarted"}


@app.delete("/api/games/{name}")
async def delete_game(name: str, authorization: Optional[str] = Header(None)):
    """Delete a game."""
    verify_token(authorization)
    validate_game_name(name)

    ports = load_ports()
    if name not in ports:
        raise HTTPException(status_code=404, detail="Game not found")

    # Stop and remove container
    cname = container_name(name)
    run_docker(["stop", cname], check=False)
    run_docker(["rm", cname], check=False)

    # Remove game directory
    game_dir = GAMES_DIR / name
    if game_dir.exists():
        shutil.rmtree(game_dir)

    # Remove port allocation
    del ports[name]
    save_ports(ports)

    return {"success": True, "message": f"Game '{name}' deleted"}


@app.get("/api/games/{name}/logs")
async def get_logs(
    name: str,
    lines: int = 50,
    authorization: Optional[str] = Header(None)
):
    """Get game server logs."""
    verify_token(authorization)
    validate_game_name(name)

    ports = load_ports()
    if name not in ports:
        raise HTTPException(status_code=404, detail="Game not found")

    result = run_docker(
        ["logs", "--tail", str(lines), container_name(name)],
        check=False
    )

    return {
        "name": name,
        "logs": result.stdout + result.stderr
    }


# ==================== Game Proxying ====================


def get_game_port(name: str) -> int:
    """Get port for a game, or raise 404."""
    ports = load_ports()
    if name not in ports:
        raise HTTPException(status_code=404, detail="Game not found")
    return ports[name]


@app.websocket("/game/{name}/ws")
async def proxy_websocket(websocket: WebSocket, name: str):
    """Proxy WebSocket connections to game containers."""
    import asyncio
    import websockets

    get_game_port(name)  # Validates game exists
    cname = container_name(name)
    await websocket.accept()

    try:
        async with websockets.connect(f"ws://{cname}:8000") as backend_ws:
            async def forward_to_backend():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await backend_ws.send(data)
                except WebSocketDisconnect:
                    pass

            async def forward_to_client():
                try:
                    async for message in backend_ws:
                        await websocket.send_text(message)
                except Exception:
                    pass

            await asyncio.gather(
                forward_to_backend(),
                forward_to_client(),
                return_exceptions=True
            )
    except Exception as e:
        try:
            await websocket.close(code=1011, reason=str(e)[:100])
        except Exception:
            pass


@app.api_route("/game/{name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_http(name: str, path: str, request: Request):
    """Proxy HTTP requests to game containers."""
    get_game_port(name)  # Validates game exists

    # Use container name since we're on the same Docker network
    # Game containers run on port 8000 internally
    cname = container_name(name)
    target_url = f"http://{cname}:8000/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    # Forward request
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get request body
        body = await request.body()

        # Forward headers (except host)
        headers = dict(request.headers)
        headers.pop("host", None)

        response = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )

        # Return response
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
        )


@app.get("/game/{name}/")
async def proxy_game_root(name: str, request: Request):
    """Proxy root path to game container."""
    get_game_port(name)  # Validates game exists
    cname = container_name(name)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"http://{cname}:8000/")
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
        )
