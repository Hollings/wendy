"""Deploy proxy endpoints for the internal API.

Extracted from :mod:`wendy.api_server` to keep that module focused on routing.
These handlers forward static-site and game deploys (and game log requests) to
the ``wendy-web`` service.
"""
from __future__ import annotations

import logging
import os
import re

import aiohttp
from aiohttp import web

_LOG = logging.getLogger(__name__)

WENDY_WEB_URL = os.getenv("WENDY_WEB_URL", "http://localhost:8910")
WENDY_DEPLOY_TOKEN = os.getenv("WENDY_DEPLOY_TOKEN", "")
WENDY_GAMES_TOKEN = os.getenv("WENDY_GAMES_TOKEN", WENDY_DEPLOY_TOKEN)


async def _read_multipart_name_and_files(request: web.Request) -> tuple[str | None, bytes | None]:
    """Parse a multipart request containing ``name`` and ``files`` fields.

    Returns ``(name, file_bytes)`` -- either value may be ``None`` if the
    corresponding field was missing.
    """
    reader = await request.multipart()
    name: str | None = None
    file_content: bytes | None = None
    async for part in reader:
        if part.name == "name":
            name = (await part.read()).decode()
        elif part.name == "files":
            file_content = await part.read()
    return name, file_content


async def _proxy_deploy(
    request: web.Request,
    *,
    token: str,
    token_env_name: str,
    deploy_path: str,
    archive_filename: str,
    timeout: int,
    extra_response_keys: tuple[str, ...] = (),
    default_message: str = "Deployed",
) -> web.Response:
    """Shared logic for proxying a deploy request to wendy-web.

    Reads a multipart ``name`` + ``files`` payload, re-packages it, and
    forwards it to *deploy_path* on the wendy-web service.
    """
    if not token:
        return web.json_response({"error": f"{token_env_name} not configured"}, status=500)

    try:
        name, file_content = await _read_multipart_name_and_files(request)
        if not name or file_content is None:
            return web.json_response({"error": "name and files fields required"}, status=400)

        form = aiohttp.FormData()
        form.add_field("name", name)
        form.add_field("files", file_content, filename=archive_filename, content_type="application/gzip")

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(
                f"{WENDY_WEB_URL}{deploy_path}",
                data=form,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    return web.json_response({"error": f"Deploy failed: {detail}"}, status=resp.status)
                result = await resp.json()

        body: dict = {
            "success": True,
            "url": result.get("url"),
            "message": result.get("message", default_message),
        }
        for key in extra_response_keys:
            body[key] = result.get(key)
        return web.json_response(body)

    except aiohttp.ClientError as e:
        return web.json_response({"error": f"Cannot connect to wendy-web: {e}"}, status=502)
    except Exception as e:
        _LOG.error("deploy error (%s): %s", deploy_path, e)
        return web.json_response({"error": "Internal server error"}, status=500)


async def handle_deploy_site(request: web.Request) -> web.Response:
    """POST /api/deploy_site -- proxy a static-site deploy to wendy-web."""
    return await _proxy_deploy(
        request,
        token=WENDY_DEPLOY_TOKEN,
        token_env_name="WENDY_DEPLOY_TOKEN",
        deploy_path="/api/sites/deploy",
        archive_filename="site.tar.gz",
        timeout=120,
        default_message="Site deployed",
    )


async def handle_deploy_game(request: web.Request) -> web.Response:
    """POST /api/deploy_game -- proxy a game deploy to wendy-web."""
    return await _proxy_deploy(
        request,
        token=WENDY_GAMES_TOKEN,
        token_env_name="WENDY_GAMES_TOKEN",
        deploy_path="/api/games/deploy",
        archive_filename="game.tar.gz",
        timeout=120,
        extra_response_keys=("ws", "port"),
        default_message="Game deployed",
    )


_GAME_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$|^[a-z0-9]$")


async def handle_game_logs(request: web.Request) -> web.Response:
    """GET /api/game_logs/{name} -- proxy game log retrieval from wendy-web.

    Query parameters:
        lines -- number of log lines to return (default 100)
    """
    name = request.match_info["name"]
    if not _GAME_NAME_RE.match(name):
        return web.json_response({"error": "Invalid game name"}, status=400)
    try:
        lines = int(request.query.get("lines", "100"))
    except ValueError:
        return web.json_response({"error": "lines must be an integer"}, status=400)

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(
                f"{WENDY_WEB_URL}/api/games/{name}/logs",
                params={"lines": lines},
                headers={"Authorization": f"Bearer {WENDY_GAMES_TOKEN}"},
            ) as resp:
                if resp.status == 404:
                    return web.json_response({"name": name, "logs": f"Game '{name}' not found"})
                if resp.status != 200:
                    return web.json_response({"name": name, "logs": f"Error: {await resp.text()}"})
                return web.json_response(await resp.json())
    except aiohttp.ClientError as e:
        return web.json_response({"name": name, "logs": f"Connection error: {e}"})
