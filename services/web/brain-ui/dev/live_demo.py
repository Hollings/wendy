"""Local read-only Brain demo connected to live Wendy over SSH.

Run from the repo: .venv/Scripts/python.exe services/web/brain-ui/dev/live_demo.py
Only DEPLOY_HOST is read from .env. Production auth secrets stay on the server.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.middleware.trustedhost import TrustedHostMiddleware

UI_DIR = Path(__file__).resolve().parents[1]
ROOT = UI_DIR.parents[2]


def deploy_host():
    value = os.getenv("DEPLOY_HOST", "")
    if not value:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("DEPLOY_HOST="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                break
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", value) or value.startswith("-"):
        raise RuntimeError("Configure DEPLOY_HOST in the repository .env")
    return value


class SSHReader:
    """One serialized SSH session, with bounded RPC waits and automatic recovery."""

    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.process = None
        self.replies = None
        self.closed = False

    def _start(self):
        bootstrap = "import sys,json; p=json.loads(sys.stdin.readline()); exec(compile(p['reader'], '<live-reader>', 'exec'), {'brain_source':p['brain'], '__name__':'__main__'})"
        command = shlex.join(["docker", "exec", "-i", "wendy-web", "python", "-u", "-c", bootstrap])
        self.process = subprocess.Popen(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
             "-o", "ServerAliveCountMax=2", self.host, command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        replies, process = queue.Queue(), self.process
        self.replies = replies

        def read_replies():
            try:
                for line in process.stdout:
                    replies.put(line)
            finally:
                replies.put(None)

        threading.Thread(target=read_replies, daemon=True).start()
        payload = {"reader": (UI_DIR / "dev/live_reader.py").read_text(encoding="utf-8"),
                   "brain": (UI_DIR.parent / "brain.py").read_text(encoding="utf-8")}
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def _stop(self):
        if self.process:
            self.process.kill()
            self.process.wait(timeout=5)
            self.process.stdin.close()
            self.process = None

    def call(self, op, **args):
        with self.lock:
            if self.closed:
                raise RuntimeError("Reader closed")
            try:
                if self.process is None or self.process.poll() is not None:
                    self._stop()
                    self._start()
                self.process.stdin.write(json.dumps({"op": op, **args}) + "\n")
                self.process.stdin.flush()
                line = self.replies.get(timeout=25)
                if not line:
                    raise RuntimeError("SSH reader disconnected")
                reply = json.loads(line)
                if not reply.get("ok"):
                    raise RuntimeError("Live read failed: " + reply.get("error", "unknown"))
                return reply["data"]
            except Exception:
                self._stop()
                raise

    def close(self):
        with self.lock:
            self.closed = True
            self._stop()


def create_app(reader, initial, ui_port=5174):
    token = secrets.token_urlsafe(32)
    state = {**initial, "last_sync": time.time(), "connected": True}
    history = deque(initial["events"], maxlen=1200)
    clients = {}
    origins = {f"http://127.0.0.1:{ui_port}", f"http://localhost:{ui_port}"}

    async def send_all(payload):
        async def send(client, lock):
            async with lock:
                await client.send_json(payload)

        for client, lock in tuple(clients.items()):
            try:
                await asyncio.wait_for(send(client, lock), 3)
            except Exception:
                clients.pop(client, None)
                with contextlib.suppress(Exception):
                    await client.close(1013)

    async def poll():
        while True:
            await asyncio.sleep(2)
            try:
                data = await asyncio.to_thread(reader.call, "poll")
                state.update(**data, last_sync=time.time(), connected=True)
                for event in data["events"]:
                    history.append(event)
                    await send_all(event)
                await send_all({"type": "channels_map", "channels": data["channels"]})
                await send_all({"type": "beads_list", "beads": data["beads"]})
            except Exception:
                state["connected"] = False
                for client in tuple(clients):
                    with contextlib.suppress(Exception):
                        await client.close(1012)
                clients.clear()

    @asynccontextmanager
    async def lifespan(app):
        worker = asyncio.create_task(poll())
        yield
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await asyncio.to_thread(reader.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    async def require_auth(request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "Local demo authentication required")

    @app.post("/api/brain/auth")
    async def authenticate(request: Request):
        if request.headers.get("origin") and request.headers["origin"] not in origins:
            raise HTTPException(403, "Local demo origin required")
        data = await request.json()
        if data.get("code") != "live":
            raise HTTPException(401, "Use the local demo access code: live")
        return {"token": token}

    @app.get("/api/brain/channels", dependencies=[Depends(require_auth)])
    async def channels():
        return {"channels": state["channels"]}

    @app.get("/api/brain/beads", dependencies=[Depends(require_auth)])
    async def beads():
        return {"beads": state["beads"]}

    @app.get("/api/brain/stats", dependencies=[Depends(require_auth)])
    async def stats():
        if not state["connected"]:
            raise HTTPException(503, "SSH connection unavailable; retrying")
        return {"viewers": len(clients), "source": "live-ssh", "last_sync": state["last_sync"]}

    @app.get("/api/brain/beads/{task_id}/log", dependencies=[Depends(require_auth)])
    async def task_log(task_id: str, offset: int = 0, log_id: str = ""):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise HTTPException(400, "Invalid task ID")
        try:
            return await asyncio.to_thread(reader.call, "task_log", task_id=task_id, offset=offset, log_id=log_id)
        except Exception:
            raise HTTPException(503, "Live task log unavailable; retrying") from None

    @app.websocket("/ws/brain")
    async def stream(socket: WebSocket):
        await socket.accept()
        if not secrets.compare_digest(socket.query_params.get("token", ""), token):
            await socket.close(4001)
            return
        if socket.headers.get("origin") and socket.headers["origin"] not in origins:
            await socket.close(1008)
            return
        if not state["connected"]:
            await socket.close(1013)
            return
        if len(clients) >= 10:
            await socket.close(4002)
            return
        lock = clients[socket] = asyncio.Lock()
        try:
            async with lock:
                await socket.send_json({"type": "channels_map", "channels": state["channels"]})
                await socket.send_json({"type": "beads_list", "beads": state["beads"]})
                for event in tuple(history):
                    await socket.send_json(event)
            while True:
                try:
                    await asyncio.wait_for(socket.receive_text(), 30)
                except TimeoutError:
                    async with lock:
                        await socket.send_json({"type": "ping"})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients.pop(socket, None)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5174, help="Local Vite port")
    parser.add_argument("--api-port", type=int, default=8001, help="Local read-only API port")
    parser.add_argument("--no-ui", action="store_true", help="Run only the local API")
    args = parser.parse_args()
    reader, ui = SSHReader(deploy_host()), None
    try:
        initial = reader.call("poll")
        print(f"Connected to live Wendy: {len(initial['events'])} recent frames, {len(initial['channels'])} channels, {len(initial['beads'])} tasks.", flush=True)
        if not args.no_ui:
            node = shutil.which("node")
            if not node:
                raise RuntimeError("Node.js is required to start Vite")
            env = {**os.environ, "BRAIN_API_URL": f"http://127.0.0.1:{args.api_port}", "VITE_BRAIN_LIVE_DEMO": "1"}
            ui = subprocess.Popen([node, "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(args.port), "--strictPort"],
                                  cwd=UI_DIR, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            time.sleep(1)
            if ui.poll() is not None:
                raise RuntimeError("Vite did not start; check the selected port and dependencies")
        print(f"Live read-only demo: http://127.0.0.1:{args.port}/  |  local access code: live", flush=True)
        print("Ctrl+C stops the local demo and its SSH connection. Production services are unchanged.", flush=True)
        uvicorn.run(create_app(reader, initial, args.port), host="127.0.0.1", port=args.api_port, log_level="warning", access_log=False)
    finally:
        reader.close()
        if ui and ui.poll() is None:
            ui.terminate()
            ui.wait(timeout=10)


if __name__ == "__main__":
    main()
