"""Pytest configuration and fixtures."""

import sys
from pathlib import Path

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# The package was renamed from `bot` to `wendy`. Register aliases so old-style
# test imports (from bot.X import Y) still resolve to the correct modules.
import wendy  # noqa: E402
import wendy.paths  # noqa: E402
import wendy.state  # noqa: E402

sys.modules.setdefault("bot", wendy)
sys.modules.setdefault("bot.paths", wendy.paths)
sys.modules.setdefault("bot.state_manager", wendy.state)

# Test files that import modules removed during the bot→wendy rename.
# Skip them at collection time rather than erroring.
_SKIP_FILES = [
    "test_claude_cli.py",
    "test_context_loader.py",
    "test_conversation.py",
    "test_fragment_loader.py",
    "test_wendy_outbox.py",
    "test_orchestrator.py",
]

collect_ignore = [str(Path(__file__).parent / f) for f in _SKIP_FILES]

# test_journal.py also imports from bot.claude_cli
collect_ignore.append(str(Path(__file__).parent / "test_journal.py"))

# test_state_manager.py tests bot.state_manager methods that no longer exist.
# test_thread_support.py internally imports bot.claude_cli at runtime.
collect_ignore.append(str(Path(__file__).parent / "test_state_manager.py"))
collect_ignore.append(str(Path(__file__).parent / "test_thread_support.py"))

# test_fragments.py and test_prompt.py import wendy.fragments which imports yaml
# (pyyaml), not installed in the CI test environment.
collect_ignore.append(str(Path(__file__).parent / "test_fragments.py"))
collect_ignore.append(str(Path(__file__).parent / "test_prompt.py"))
