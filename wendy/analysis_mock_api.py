"""Per-fork mock HTTP API for !analysis runs.

When `!analysis` spawns a forked Claude CLI, that CLI must believe it is
talking to the real internal API server (so it acts exactly like real
Wendy: calls `msgs`, sees the variant prompt, replies with `msg`). This
module spins up a minimal aiohttp app per fork that:

- Serves `GET /api/check_messages/{channel_id}` with a canned message list
  (mirroring the real shape from `wendy.api_server.handle_check_messages`).
- Captures `POST /api/send_message` payloads to a list instead of posting
  to Discord.
- Returns 503 stubs for every other endpoint a fork might wander into,
  so a curl never hangs against a missing route.

Each fork gets its own server bound to ``127.0.0.1:0`` (kernel-assigned
free port), returned to the caller for env injection
(``WENDY_PROXY_PORT``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

_LOG = logging.getLogger(__name__)


@dataclass
class MockApiHandle:
    """Handle returned by ``start()`` for controlling a running mock server."""

    port: int
    captured_msgs: list[dict[str, Any]]
    unknown_endpoint_hits: list[str]
    runner: web.AppRunner = field(repr=False)

    async def stop(self) -> None:
        await self.runner.cleanup()


def _make_app(
    fake_messages: list[dict[str, Any]],
    captured_msgs: list[dict[str, Any]],
    unknown_endpoint_hits: list[str],
) -> web.Application:
    """Build the aiohttp application with all stub routes."""

    state = {"sent_count": 0}

    async def check_messages(request: web.Request) -> web.Response:
        # Return the variant before any send_message, then empty after.
        # If the model loops on msgs it just keeps seeing the same variant
        # (better than seeing empty, which makes it bypass the helper and
        # try raw curl against the real api on port 8945).
        if state["sent_count"] == 0:
            return web.json_response(
                {"messages": list(fake_messages), "task_updates": []}
            )
        return web.json_response({"messages": [], "task_updates": []})

    async def send_message(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        captured_msgs.append(body)
        state["sent_count"] += 1

        # Fabricate a plausible response shape so the fork's `msg` helper
        # exits 0 and Wendy treats the send as successful.
        fake_id = 9_500_000_000_000_000_000 + len(captured_msgs)
        text = body.get("content") or body.get("message") or ""
        return web.json_response({
            "success": True,
            "message": "Message sent",
            "message_id": fake_id,
            "content": text,
            "new_messages": [],
        })

    async def emojis(request: web.Request) -> web.Response:
        return web.json_response({"emojis": []})

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def stub_503(request: web.Request) -> web.Response:
        path = request.path
        unknown_endpoint_hits.append(path)
        _LOG.info("mock api blocked endpoint: %s", path)
        return web.json_response(
            {"error": f"endpoint disabled during !analysis: {path}"},
            status=503,
        )

    app = web.Application()
    app.router.add_get(r"/api/check_messages/{channel_id}", check_messages)
    app.router.add_post("/api/send_message", send_message)
    app.router.add_get("/api/emojis", emojis)
    app.router.add_get("/health", health)

    # Catch-all stubs for endpoints we don't want forks reaching.
    for path in (
        "/api/deploy_site",
        "/api/deploy_game",
        "/api/analyze_file",
        "/api/active_beads",
        "/api/cancel_bead",
        "/api/usage",
        "/api/usage/refresh",
        "/api/schedule_wake",
    ):
        app.router.add_route("*", path, stub_503)

    # Wildcard for anything else.
    app.router.add_route("*", "/{tail:.*}", stub_503)

    return app


async def start(fake_messages: list[dict[str, Any]]) -> MockApiHandle:
    """Start a mock API on a free localhost port. Caller must call ``stop()``."""
    captured_msgs: list[dict[str, Any]] = []
    unknown_endpoint_hits: list[str] = []
    app = _make_app(fake_messages, captured_msgs, unknown_endpoint_hits)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    # Pull the actual port the kernel assigned.
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    _LOG.info("mock api started on 127.0.0.1:%d", port)
    return MockApiHandle(
        port=port,
        captured_msgs=captured_msgs,
        unknown_endpoint_hits=unknown_endpoint_hits,
        runner=runner,
    )
