"""Read-only RPC worker, sent over SSH and executed in memory by live_demo.py.

Uses the checkout's Brain readers against the live data volume. No services,
files, task state, or production credentials are changed.
"""
import hashlib
import json
import sys
import time
import types
from collections import deque


def serve(source):
    brain = types.ModuleType("live_brain_readers")
    exec(compile(source, "<checkout-brain.py>", "exec"), brain.__dict__)
    seen, order = set(), deque()
    positions = {path.name: path.stat().st_size for path in brain.ORCHESTRATOR_LOGS_DIR.glob("agent_*.log")}

    def poll():
        events = []
        for line in brain.get_recent_events(1200):
            key = hashlib.sha256(line.encode()).digest()
            if key in seen:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen.add(key)
            order.append(key)
            if len(order) > 6000:
                seen.remove(order.popleft())
            events.append(event)
        latest = {}
        for path in brain.ORCHESTRATOR_LOGS_DIR.glob("agent_*.log"):
            task_id = brain._extract_task_id(path.name)
            if task_id and (task_id not in latest or path.stat().st_mtime_ns > latest[task_id].stat().st_mtime_ns):
                latest[task_id] = path
        for task_id, path in latest.items():
            if positions.get(path.name) == path.stat().st_size:
                continue
            result = brain.read_task_log(task_id, positions.get(path.name, 0), path.name)
            positions[path.name] = result["offset"]
            for line in result["log"].splitlines():
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                event = raw.get("event", raw)
                if not isinstance(event, dict):
                    continue
                ts = raw.get("ts") or event.get("timestamp")
                events.append({"event": event, "ts": ts or int(time.time() * 1000),
                               "channel_id": None, "bead_id": task_id,
                               "attempt_id": result.get("attempt_id"), "timestamp_estimated": not bool(ts)})
        return {"events": events, "channels": brain.get_channels_map(), "beads": brain._read_beads_list()}

    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request["op"] == "poll":
                result = poll()
            elif request["op"] == "task_log":
                result = brain.read_task_log(request["task_id"], int(request.get("offset", 0)), request.get("log_id", ""))
            else:
                raise ValueError("Unsupported read operation")
            print(json.dumps({"ok": True, "data": result}), flush=True)
        except Exception as error:
            print(json.dumps({"ok": False, "error": type(error).__name__}), flush=True)


if __name__ == "__main__":
    serve(brain_source)  # noqa: F821 — supplied by the SSH bootstrap
