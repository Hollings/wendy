"""Local-only fixture API using the real brain/auth handlers. No production data."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ui_dir = Path(__file__).resolve().parents[1]
project = ui_dir.parents[2]
preview = project / ".venv" / "brain-preview"
preview.mkdir(parents=True, exist_ok=True)
os.environ.update(BRAIN_ACCESS_CODE="preview", BRAIN_SECRET="local-preview-only",
                  SITES_DIR=str(preview / "sites"), GAMES_DIR=str(preview / "games"),
                  WENDY_DB_PATH=str(preview / "unused.db"))
sys.path.insert(0, str(ui_dir.parent))
import brain  # noqa: E402
import main  # noqa: E402
import uvicorn  # noqa: E402

fixture = json.loads(subprocess.check_output(
    ["node", "--input-type=module", "-e",
     "import {frames,channels,tasks} from './src/dev/fixtures.js'; console.log(JSON.stringify({frames,channels,tasks}))"],
    cwd=ui_dir, text=True, encoding="utf-8"))
brain.STREAM_FILE = preview / "stream.jsonl"
brain.BEADS_SNAPSHOT = preview / "beads.json"
brain.ORCHESTRATOR_LOGS_DIR = preview / "logs"
brain.ORCHESTRATOR_LOGS_DIR.mkdir(exist_ok=True)
brain.CLAUDE_DIR = preview / "claude"
brain.get_channels_map = lambda: fixture["channels"]
brain.STREAM_FILE.write_text("\n".join(json.dumps(frame) for frame in fixture["frames"]) + "\n", encoding="utf-8")
brain.BEADS_SNAPSHOT.write_text(json.dumps(fixture["tasks"]), encoding="utf-8")
task_lines = [json.dumps(frame["event"]) for frame in fixture["frames"] if frame.get("bead_id") == "wd-41"]
(brain.ORCHESTRATOR_LOGS_DIR / "agent_wd-41_preview.log").write_text("\n".join(task_lines) + "\n", encoding="utf-8")


@main.app.on_event("startup")
async def preview_controls():
    async def controls():
        flag = preview / "disconnect"
        while True:
            if flag.exists():
                flag.unlink()
                for socket in list(brain.connected_clients):
                    await socket.close(1001)
            await asyncio.sleep(0.2)
    asyncio.create_task(controls())


if __name__ == "__main__":
    print("Fictional preview only. Open http://127.0.0.1:5173 and use access code: preview")
    uvicorn.run(main.app, host="127.0.0.1", port=8000, log_level="warning")
