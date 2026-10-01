import importlib.util
import json
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("viewer_brain", Path(__file__).parents[1] / "brain.py")
brain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brain)


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "ORCHESTRATOR_LOGS_DIR", tmp_path)
    return tmp_path


def test_new_task_fields_survive_snapshot_and_transient_corruption(tmp_path, monkeypatch):
    snapshot = tmp_path / "tasks.json"
    monkeypatch.setattr(brain, "BEADS_SNAPSHOT", snapshot)
    monkeypatch.setattr(brain, "_last_good_beads", [])
    snapshot.write_text(json.dumps([{"id": "wd-1", "phase": "quota_wait", "model": "claude-fable-5-1", "_channel": "coding", "close_reason": "Quota reached"}]))
    tasks = brain._read_beads_list()
    assert tasks[0]["phase"] == "quota_wait"
    assert tasks[0]["model"] == "claude-fable-5-1"
    assert tasks[0]["_channel"] == "coding"
    snapshot.write_text("{")
    assert brain._read_beads_list() == tasks


def test_task_ids_with_underscores_are_not_truncated(logs):
    (logs / "agent_my_task_abc.log").write_bytes(b'{"type":"result"}\n')
    (logs / "agent_my_task_other_xyz.log").write_text('{"wrong":true}\n')
    assert brain._extract_task_id("agent_my_task_abc.log") == "my_task"
    result = brain.read_task_log("my_task")
    assert result["log"] == '{"type":"result"}\n'
    assert result["complete"] is True


def test_incremental_read_preserves_partial_utf8_line(logs):
    path = logs / "agent_wd-1_first.log"
    first = '{"text":"hello 🌱"}\n'.encode()
    path.write_bytes(first + b'{"type":"res')
    result = brain.read_task_log("wd-1")
    assert result["offset"] == len(first)
    assert result["log"] == first.decode()
    path.write_bytes(first + b'{"type":"result"}\n')
    next_read = brain.read_task_log("wd-1", result["offset"], result["log_id"])
    assert next_read["log"] == '{"type":"result"}\n'
    assert next_read["complete"] is True


def test_new_attempt_resets_cursor_even_when_new_file_is_larger(logs):
    first = logs / "agent_wd-1_first.log"
    first.write_text('{"old":true}\n')
    result = brain.read_task_log("wd-1")
    second = logs / "agent_wd-1_second.log"
    second.write_text('{"new":"this attempt has a longer first event"}\n')
    os.utime(second, (first.stat().st_mtime + 10,) * 2)
    next_read = brain.read_task_log("wd-1", result["offset"], result["log_id"])
    assert next_read["log"].startswith('{"new"')
    assert next_read["attempt_id"] == "second"


def test_large_logs_return_bounded_tail(logs):
    path = logs / "agent_wd-1_first.log"
    path.write_bytes(b'{"line":"old event"}\n' * 30000 + b'{"type":"result"}\n')
    result = brain.read_task_log("wd-1")
    assert len(result["log"].encode()) <= 256 * 1024
    assert result["truncated"] is True
    assert result["offset"] == path.stat().st_size
    assert result["complete"] is True


def test_glob_and_path_traversal_are_rejected(logs):
    for task in ("../private", "*", "wd-?", "a/b"):
        with pytest.raises(ValueError):
            brain.read_task_log(task)


def test_missing_log_is_an_explicit_empty_state(logs):
    assert brain.read_task_log("missing")["log"] == ""


def test_repeated_poll_does_not_repeat_completed_log(logs):
    (logs / "agent_wd-1_first.log").write_text('{"type":"result"}\n')
    result = brain.read_task_log("wd-1")
    repeated = brain.read_task_log("wd-1", result["offset"], result["log_id"])
    assert repeated["log"] == ""
    assert repeated["offset"] == result["offset"]
    assert repeated["complete"] is True
    assert brain.read_task_log("wd-1", result["offset"])["log"] == ""
