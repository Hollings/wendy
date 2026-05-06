"""Smoke-test the !analysis pipeline against the latest real bot message.

Runs INSIDE the wendy container (so it sees the real DB, session JSONLs, and
the Claude CLI). Picks the latest bot message that has a nudge_id, then
calls analysis.run_analysis() directly -- no Discord.

Usage (from the host):
    ssh ubuntu@<pi> 'docker exec wendy python /app/scripts/analysis_smoke.py'

Optional: pass a specific message_id as argv[1] to analyze a particular reply.
"""
from __future__ import annotations

import os
import sys

# Python auto-adds the script's directory to sys.path[0]. /app/scripts/
# contains a `secrets.py` (the custom secrets-manager CLI), which would
# shadow the stdlib `secrets` module that wendy.analysis imports. Drop the
# script dir from sys.path before any wendy imports.
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir in sys.path:
    sys.path.remove(_script_dir)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

import asyncio  # noqa: E402
import logging  # noqa: E402

from wendy import analysis  # noqa: E402
from wendy.config import parse_channel_configs  # noqa: E402
from wendy.state import state as state_manager  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_LOG = logging.getLogger("analysis_smoke")


def pick_target() -> tuple[int, int, str]:
    """Return (target_msg_id, channel_id, channel_folder)."""
    if len(sys.argv) > 1:
        target_msg_id = int(sys.argv[1])
        row = state_manager._get_conn().execute(
            "SELECT channel_id FROM message_history WHERE message_id = ?",
            (target_msg_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"message {target_msg_id} not found")
        channel_id = row["channel_id"]
    else:
        # Pick the latest bot message that has a nudge_id.
        row = state_manager._get_conn().execute(
            "SELECT message_id, channel_id FROM message_history "
            "WHERE is_bot = 1 AND nudge_id IS NOT NULL "
            "ORDER BY message_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise SystemExit(
                "no bot messages with nudge_id yet -- send a message in a "
                "channel and let Wendy reply, then retry."
            )
        target_msg_id = row["message_id"]
        channel_id = row["channel_id"]

    # Resolve channel folder via channel config.
    configs = parse_channel_configs()
    cfg = configs.get(channel_id)
    if cfg is None:
        raise SystemExit(
            f"channel_id {channel_id} not in WENDY_CHANNEL_CONFIG"
        )
    folder = cfg.get("_folder") or cfg.get("name") or "default"
    return target_msg_id, channel_id, folder


async def main() -> None:
    target_msg_id, channel_id, folder = pick_target()
    _LOG.info("smoke: analyzing msg %s in channel %s (folder=%s)",
              target_msg_id, channel_id, folder)

    async def progress(text: str) -> None:
        _LOG.info("progress: %s", text)

    try:
        output = await analysis.run_analysis(
            channel_id=channel_id,
            channel_name=folder,
            target_msg_id=target_msg_id,
            target_msg_link=None,
            on_progress=progress,
        )
    except analysis.AnalysisError as e:
        _LOG.error("analysis error: %s", e)
        raise SystemExit(2)
    except Exception:
        _LOG.exception("analysis crashed")
        raise SystemExit(3)

    print()
    print("=" * 78)
    print(output)
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
