"""Fragment directory seeding.

Called once at startup. Copies config/claude_fragments/ to /data/wendy/claude_fragments/.
Never overwrites existing files.

No migration code -- clean break from v1.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .paths import FRAGMENTS_DIR

_LOG = logging.getLogger(__name__)


def setup_fragments_dir() -> None:
    """Seed fragment files from /app/config/claude_fragments/ to FRAGMENTS_DIR.

    Only copies files that don't already exist, preserving runtime edits.
    """
    src_dir = Path("/app/config/claude_fragments")
    if not src_dir.exists():
        _LOG.info("No source fragments dir at %s, skipping seed", src_dir)
        return

    FRAGMENTS_DIR.mkdir(parents=True, exist_ok=True)

    for src_file in src_dir.rglob("*"):
        if not src_file.is_file():
            continue

        rel_path = src_file.relative_to(src_dir)
        dest_file = FRAGMENTS_DIR / rel_path

        if dest_file.exists():
            continue

        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        _LOG.info("Seeded fragment file: %s", rel_path)
