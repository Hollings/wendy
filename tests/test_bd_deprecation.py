"""Deprecating the public command must not corrupt the controller's backend."""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from wendy import beads
from wendy.cli import _sync_scripts


def test_public_bd_does_not_execute_legacy_mutations(tmp_path):
    marker = tmp_path / 'unexpected-mutation'
    backend = tmp_path / 'backend.py'
    backend.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / 'bin/bd'), 'close', 'existing-task'],
        env={**os.environ, 'WENDY_BD_BINARY': str(backend)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert not result.stdout
    assert 'deprecated' in result.stderr and 'wtask' in result.stderr
    assert not marker.exists()


@pytest.mark.skipif(os.name != 'posix', reason='Production npm executable symlinks require Linux')
def test_installing_public_shim_preserves_npm_backend_even_if_newer(tmp_path):
    source, installed = tmp_path / 'source', tmp_path / 'installed'
    source.mkdir()
    installed.mkdir()
    backend = tmp_path / 'npm-bd.js'
    backend.write_text('original backend')
    (installed / 'bd').symlink_to(backend)
    (source / 'bd').write_text('deprecated command')
    os.utime(source / 'bd', (1, 1))
    _sync_scripts(source, installed, '*', make_executable=True)
    assert backend.read_text() == 'original backend'
    assert not (installed / 'bd').is_symlink()
    assert (installed / 'bd').read_text() == 'deprecated command'
    assert os.access(installed / 'bd', os.X_OK)


@pytest.mark.asyncio
async def test_controller_bypasses_public_bd_command(monkeypatch, tmp_path):
    monkeypatch.delenv('WENDY_BD_BINARY', raising=False)
    monkeypatch.setattr(beads, 'channel_dir', lambda _: tmp_path)
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate.return_value = (b'[]', b'')
    launch = AsyncMock(return_value=proc)
    monkeypatch.setattr(beads.asyncio, 'create_subprocess_exec', launch)
    assert await beads.BeadsClient().ready('coding') == []
    assert launch.call_args.args[0] == '/usr/local/libexec/wendy-bd'
