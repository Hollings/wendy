"""Tests for the beads task runner (wendy/tasks.py)."""

import json

from wendy.tasks import (
    AGENT_PROMPT_TEMPLATE,
    RUNNER_ASSIGNEE,
    extract_result_summary,
    tasks_to_reopen,
)

# ---------------------------------------------------------------------------
# tasks_to_reopen (stuck-task sweep decision logic)
# ---------------------------------------------------------------------------

def test_reopens_orphaned_runner_claimed_task():
    in_progress = [{"id": "c-abc", "assignee": RUNNER_ASSIGNEE}]
    assert tasks_to_reopen(in_progress, running_task_ids=set()) == ["c-abc"]


def test_reopens_orphaned_unassigned_task():
    in_progress = [{"id": "c-abc"}, {"id": "c-def", "assignee": ""}]
    assert tasks_to_reopen(in_progress, set()) == ["c-abc", "c-def"]


def test_leaves_running_agents_alone():
    in_progress = [{"id": "c-abc", "assignee": RUNNER_ASSIGNEE}]
    assert tasks_to_reopen(in_progress, {"c-abc"}) == []


def test_leaves_human_claimed_tasks_alone():
    in_progress = [{"id": "c-abc", "assignee": "hollings"}]
    assert tasks_to_reopen(in_progress, set()) == []


def test_ignores_tasks_without_id():
    assert tasks_to_reopen([{"assignee": RUNNER_ASSIGNEE}], set()) == []


# ---------------------------------------------------------------------------
# extract_result_summary (stream-json log parsing)
# ---------------------------------------------------------------------------

def _write_log(tmp_path, lines):
    log = tmp_path / "agent_test.log"
    log.write_text("\n".join(lines), encoding="utf-8")
    return log


def test_extracts_final_result(tmp_path):
    lines = [
        "Task: c-abc - test",
        json.dumps({"type": "assistant", "text": "working"}),
        json.dumps({"type": "result", "subtype": "success", "result": "Fixed the bug in game.js"}),
    ]
    assert extract_result_summary(_write_log(tmp_path, lines)) == "Fixed the bug in game.js"


def test_uses_last_result_event(tmp_path):
    lines = [
        json.dumps({"type": "result", "result": "first"}),
        json.dumps({"type": "result", "result": "second"}),
    ]
    assert extract_result_summary(_write_log(tmp_path, lines)) == "second"


def test_truncates_long_results(tmp_path):
    lines = [json.dumps({"type": "result", "result": "x" * 5000})]
    out = extract_result_summary(_write_log(tmp_path, lines))
    assert len(out) <= 700
    assert out.endswith("...")


def test_empty_when_no_result_event(tmp_path):
    lines = [json.dumps({"type": "assistant", "text": "hi"})]
    assert extract_result_summary(_write_log(tmp_path, lines)) == ""


def test_empty_when_log_missing(tmp_path):
    assert extract_result_summary(tmp_path / "nope.log") == ""


def test_skips_malformed_result_lines(tmp_path):
    lines = [
        json.dumps({"type": "result", "result": "good"}),
        '{"type":"result","result": TRUNCATED',
    ]
    assert extract_result_summary(_write_log(tmp_path, lines)) == "good"


# ---------------------------------------------------------------------------
# AGENT_PROMPT_TEMPLATE formatting
# ---------------------------------------------------------------------------

def test_prompt_template_formats_cleanly():
    prompt = AGENT_PROMPT_TEMPLATE.format(
        task_id="c-abc",
        title="fix the thing",
        description="details here",
        workdir="/data/wendy/channels/coding",
    )
    assert "TASK ID: c-abc" in prompt
    assert "bd done c-abc" in prompt
    assert "/data/wendy/channels/coding" in prompt


def test_prompt_template_survives_braces_in_user_fields():
    # Braces inside substituted values are literal -- no escaping needed,
    # and none should be applied (escaping would corrupt them to {{...}}).
    prompt = AGENT_PROMPT_TEMPLATE.format(
        task_id="c-abc", title="task about {task_id} placeholders",
        description="d", workdir="/w",
    )
    assert "task about {task_id} placeholders" in prompt
