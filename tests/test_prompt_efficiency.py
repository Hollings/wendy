"""Runtime behavior of bounded context and optional memory reminders."""
import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

from wendy import memory_reminders as memory
from wendy import prompt


def test_journal_index_is_bounded_and_searchable(tmp_path, monkeypatch):
    for i in range(40):
        (tmp_path / f'{i:02d}.md').write_text('note')
    monkeypatch.setattr(prompt, 'journal_dir', lambda _: tmp_path)
    listing = prompt.get_journal_listing_for_nudge('thread')
    assert '40 files' in listing
    assert listing.count('.md') == 12
    assert f'Search {tmp_path}/' in listing


def test_memory_write_and_reminders_are_conversation_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, 'SHARED_DIR', tmp_path)
    journal = tmp_path / 'journal'
    journal.mkdir()
    now = [100.0]
    monkeypatch.setattr(memory.time, 'time', lambda: now[0])
    for _ in range(24):
        assert memory.reminder(1, journal) == ''
        assert memory.reminder(2, journal) == ''
    now[0] += 11000
    memory.record_write(1)
    assert memory.reminder(1, journal) == ''
    assert 'Optional memory check' in memory.reminder(2, journal)
    assert memory.reminder(2, journal) == ''


def test_journal_write_through_bash_postpones_reminder(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(memory, 'SHARED_DIR', tmp_path)
    journal = tmp_path / 'journal'
    journal.mkdir()
    monkeypatch.setattr(memory.time, 'time', lambda: 100.0)
    for _ in range(24):
        memory.reminder(1, journal)
    note = journal / 'new.md'
    note.write_text('learned something')
    os.utime(note, (11000, 11000))
    monkeypatch.setattr(memory.time, 'time', lambda: 11100.0)
    assert memory.reminder(1, journal) == ''


def test_boundary_dispatches_only_to_matching_role(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / 'config/hooks'
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location('boundary_test', directory / 'boundary.py')
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    event = {'hook_event_name': 'Stop'}
    with mock.patch.object(hook.conversation_delivery, 'main') as main, mock.patch.object(hook.task_mailbox, 'main') as worker:
        monkeypatch.delenv('WENDY_TASK_TOKEN', raising=False)
        monkeypatch.setattr(sys, 'stdin', __import__('io').StringIO(json.dumps(event)))
        hook.main()
        main.assert_called_once_with(event)
        worker.assert_not_called()
        monkeypatch.setenv('WENDY_TASK_TOKEN', 'worker')
        monkeypatch.setattr(sys, 'stdin', __import__('io').StringIO(json.dumps(event)))
        hook.main()
        worker.assert_called_once_with(event)
        assert main.call_count == 1


def test_stop_has_no_memory_hooks_and_keeps_image_analysis():
    config = json.loads((Path(__file__).resolve().parents[1] / 'config/claude_settings.json').read_text())
    stop = config['hooks']['Stop']
    assert len(stop) == 1
    assert 'boundary.py' in stop[0]['hooks'][0]['command']
    assert any('remind_analyze_file.sh' in hook['command'] for entry in config['hooks']['PostToolUse'] for hook in entry['hooks'])
