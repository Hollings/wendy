"""Tests for the brain feed's incremental line tailing (services/web/brain.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "web"))

from brain import read_complete_lines  # noqa: E402


def write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


def test_reads_complete_lines(tmp_path):
    f = tmp_path / "stream.jsonl"
    write_bytes(f, b'{"a":1}\n{"b":2}\n')
    lines, pos = read_complete_lines(f, 0)
    assert lines == ['{"a":1}', '{"b":2}']
    assert pos == f.stat().st_size


def test_partial_trailing_line_left_in_file(tmp_path):
    f = tmp_path / "stream.jsonl"
    write_bytes(f, b'{"a":1}\n{"b":')
    lines, pos = read_complete_lines(f, 0)
    assert lines == ['{"a":1}']
    assert pos == len(b'{"a":1}\n')

    # Writer finishes the line; next read picks up the whole thing
    with open(f, "ab") as fh:
        fh.write(b'2}\n')
    lines, pos = read_complete_lines(f, pos)
    assert lines == ['{"b":2}']
    assert pos == f.stat().st_size


def test_no_newline_at_all_returns_nothing(tmp_path):
    f = tmp_path / "stream.jsonl"
    write_bytes(f, b'{"partial":')
    lines, pos = read_complete_lines(f, 0)
    assert lines == []
    assert pos == 0


def test_resumes_from_offset(tmp_path):
    f = tmp_path / "stream.jsonl"
    first = b'{"a":1}\n'
    write_bytes(f, first + b'{"b":2}\n{"c":3}\n')
    lines, pos = read_complete_lines(f, len(first))
    assert lines == ['{"b":2}', '{"c":3}']
    assert pos == f.stat().st_size


def test_empty_and_blank_lines_skipped(tmp_path):
    f = tmp_path / "stream.jsonl"
    write_bytes(f, b'\n  \n{"a":1}\n\n')
    lines, pos = read_complete_lines(f, 0)
    assert lines == ['{"a":1}']
    assert pos == f.stat().st_size


def test_read_at_eof(tmp_path):
    f = tmp_path / "stream.jsonl"
    write_bytes(f, b'{"a":1}\n')
    size = f.stat().st_size
    lines, pos = read_complete_lines(f, size)
    assert lines == []
    assert pos == size
