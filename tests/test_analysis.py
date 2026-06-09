"""Tests for wendy.analysis.

Covers the bits we can test without standing up the full Claude CLI:
  - JSONL truncation: drops lines from the nudge marker forward, rewrites UUIDs
  - Fork-point resolver: surfaces the right errors and the right line index
  - Mock API: response shape matches real api_server, captures sends, stubs unknowns

The end-to-end fork execution path is exercised by the smoke test in
``scripts/analysis_smoke.py``, not here -- it needs a live Claude CLI.
"""
from __future__ import annotations

import json

import pytest

from wendy import analysis, analysis_mock_api
from wendy.state import StateManager

# =========================================================================
# Fixtures
# =========================================================================


def _make_sm(tmp_path) -> StateManager:
    sm = StateManager(db_path=tmp_path / "test.db")
    sm._get_conn()
    return sm


# =========================================================================
# JSONL truncation
# =========================================================================


def test_truncate_jsonl_drops_nudge_forward(tmp_path):
    parent_uuid = "00000000-aaaa-aaaa-aaaa-000000000001"
    fork_uuid = "ffffffff-bbbb-bbbb-bbbb-ffffffffffff"

    parent_path = tmp_path / f"{parent_uuid}.jsonl"
    parent_path.write_text(
        "\n".join([
            json.dumps({"type": "user", "session_id": parent_uuid, "content": "first turn"}),
            json.dumps({"type": "assistant", "session_id": parent_uuid, "content": "reply 1"}),
            json.dumps({"type": "user", "session_id": parent_uuid, "content": "<nudge>\n[nudge:abc12345]"}),
            json.dumps({"type": "assistant", "session_id": parent_uuid, "content": "reply 2"}),
            json.dumps({"type": "result", "session_id": parent_uuid}),
        ]) + "\n",
        encoding="utf-8",
    )

    fork_path = tmp_path / f"{fork_uuid}.jsonl"
    analysis._truncate_jsonl(
        parent_jsonl=parent_path,
        fork_jsonl=fork_path,
        parent_uuid=parent_uuid,
        fork_uuid=fork_uuid,
        nudge_line_idx=2,  # the line with [nudge:abc12345]
    )

    out = fork_path.read_text(encoding="utf-8")
    assert parent_uuid not in out, "parent UUID should be rewritten"
    assert fork_uuid in out, "fork UUID should be present"
    assert "first turn" in out, "events before truncation point should remain"
    assert "[nudge:abc12345]" not in out, "nudge line + everything after should be dropped"
    assert "reply 2" not in out, "events after the nudge should be dropped"
    # Two lines retained.
    assert out.strip().count("\n") == 1


def test_truncate_jsonl_at_index_zero(tmp_path):
    parent_uuid = "00000000-aaaa-aaaa-aaaa-000000000001"
    fork_uuid = "11111111-bbbb-bbbb-bbbb-111111111111"
    parent_path = tmp_path / f"{parent_uuid}.jsonl"
    parent_path.write_text(
        json.dumps({"type": "user", "session_id": parent_uuid}) + "\n",
        encoding="utf-8",
    )
    fork_path = tmp_path / f"{fork_uuid}.jsonl"
    analysis._truncate_jsonl(
        parent_jsonl=parent_path, fork_jsonl=fork_path,
        parent_uuid=parent_uuid, fork_uuid=fork_uuid,
        nudge_line_idx=0,
    )
    assert fork_path.read_text(encoding="utf-8") == ""


# =========================================================================
# Fork-point resolution
# =========================================================================


def test_resolve_fork_point_rejects_user_message(tmp_path, monkeypatch):
    sm = _make_sm(tmp_path)
    monkeypatch.setattr(analysis, "state_manager", sm)
    sm.insert_message(
        message_id=1, channel_id=100, guild_id=None,
        author_id=42, author_nickname="alice", is_bot=False,
        content="not from wendy", timestamp=1000,
    )
    with pytest.raises(analysis.AnalysisError, match="my own messages"):
        analysis._resolve_fork_point(100, "general", 1)


def test_resolve_fork_point_rejects_missing_message(tmp_path, monkeypatch):
    sm = _make_sm(tmp_path)
    monkeypatch.setattr(analysis, "state_manager", sm)
    with pytest.raises(analysis.AnalysisError, match="can't find"):
        analysis._resolve_fork_point(100, "general", 99999)


def test_resolve_fork_point_rejects_missing_nudge_id(tmp_path, monkeypatch):
    sm = _make_sm(tmp_path)
    monkeypatch.setattr(analysis, "state_manager", sm)
    sm.insert_message(
        message_id=2, channel_id=100, guild_id=None,
        author_id=999, author_nickname="wendy", is_bot=True,
        content="ancient reply", timestamp=1000,  # no nudge_id
    )
    with pytest.raises(analysis.AnalysisError, match="predates the nudge"):
        analysis._resolve_fork_point(100, "general", 2)


def test_resolve_fork_point_rejects_wrong_channel(tmp_path, monkeypatch):
    sm = _make_sm(tmp_path)
    monkeypatch.setattr(analysis, "state_manager", sm)
    sm.insert_message(
        message_id=3, channel_id=200, guild_id=None,
        author_id=999, author_nickname="wendy", is_bot=True,
        content="reply", timestamp=1000, nudge_id="abc12345",
    )
    with pytest.raises(analysis.AnalysisError, match="different channel"):
        analysis._resolve_fork_point(100, "general", 3)


def test_resolve_fork_point_finds_marker(tmp_path, monkeypatch):
    sm = _make_sm(tmp_path)
    monkeypatch.setattr(analysis, "state_manager", sm)

    # Setup: user message + bot reply with nudge_id in DB
    sm.insert_message(
        message_id=10, channel_id=100, guild_id=None,
        author_id=42, author_nickname="alice", is_bot=False,
        content="i love dogs", timestamp=1000,
    )
    sm.insert_message(
        message_id=11, channel_id=100, guild_id=None,
        author_id=999, author_nickname="wendy", is_bot=True,
        content="dogs are great", timestamp=1001, nudge_id="deadbeef",
    )

    # Fake the session_dir lookup -- create a JSONL with the marker
    sess_uuid = "12345678-aaaa-bbbb-cccc-1234567890ab"
    fake_session_dir = tmp_path / "sessions"
    fake_session_dir.mkdir()
    jsonl = fake_session_dir / f"{sess_uuid}.jsonl"
    jsonl.write_text(
        "\n".join([
            json.dumps({"type": "user", "content": "earlier turn"}),
            json.dumps({"type": "user", "content": "<nudge>\n[nudge:deadbeef]"}),
            json.dumps({"type": "assistant", "content": "dogs are great"}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        analysis, "session_dir",
        lambda channel_name: fake_session_dir,
    )

    fp = analysis._resolve_fork_point(100, "general", 11)
    assert fp.parent_session_id == sess_uuid
    assert fp.parent_jsonl == jsonl
    assert fp.nudge_line_idx == 1
    assert fp.prompting_msg_id == 10
    assert fp.prompting_content == "i love dogs"
    assert fp.target_response == "dogs are great"


# =========================================================================
# Mock API
# =========================================================================


@pytest.mark.asyncio
async def test_mock_api_check_messages_returns_canned():
    fake = [{
        "message_id": 999,
        "author": "alice",
        "is_bot": False,
        "content": "hi",
        "timestamp": 1000,
    }]
    handle = await analysis_mock_api.start(fake)
    try:
        async with _aiohttp_client(handle.port) as session:
            # Repeated msgs calls before any msg send all return the variant
            # (so models that loop on msgs don't bypass the helper for raw curl).
            for _ in range(3):
                async with session.get("/api/check_messages/123") as resp:
                    assert resp.status == 200
                    assert (await resp.json()) == {"messages": fake, "task_updates": []}
            # After a send_message, subsequent msgs calls return empty.
            async with session.post(
                "/api/send_message",
                json={"channel_id": "123", "content": "ok"},
            ) as resp:
                assert resp.status == 200
            async with session.get("/api/check_messages/123") as resp:
                assert (await resp.json()) == {"messages": [], "task_updates": []}
    finally:
        await handle.stop()


@pytest.mark.asyncio
async def test_mock_api_captures_send_message():
    handle = await analysis_mock_api.start([])
    try:
        async with _aiohttp_client(handle.port) as session:
            async with session.post(
                "/api/send_message",
                json={"channel_id": "123", "content": "hello world"},
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
        assert data["success"] is True
        assert data["content"] == "hello world"
        assert data["message_id"] >= 9_500_000_000_000_000_000
        assert len(handle.captured_msgs) == 1
        assert handle.captured_msgs[0]["content"] == "hello world"
    finally:
        await handle.stop()


@pytest.mark.asyncio
async def test_mock_api_stubs_unknown_endpoints():
    handle = await analysis_mock_api.start([])
    try:
        async with _aiohttp_client(handle.port) as session:
            async with session.post("/api/deploy_site") as resp:
                assert resp.status == 503
            async with session.get("/api/some_random_thing") as resp:
                assert resp.status == 503
        assert "/api/deploy_site" in handle.unknown_endpoint_hits
        assert any("some_random" in h for h in handle.unknown_endpoint_hits)
    finally:
        await handle.stop()


# Helper: aiohttp test client against a live mock server.
class _aiohttp_client:
    def __init__(self, port: int) -> None:
        self.port = port
        self._session = None

    async def __aenter__(self):
        import aiohttp
        self._session = aiohttp.ClientSession(
            base_url=f"http://127.0.0.1:{self.port}"
        )
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        await self._session.close()
