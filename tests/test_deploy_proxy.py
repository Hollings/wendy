"""Tests for wendy.deploy_proxy game-name validation.

``_GAME_NAME_RE`` guards the path segment interpolated into the wendy-web
``/api/games/{name}/logs`` URL, so it must reject anything that could escape
the intended path (slashes, dots, whitespace) or otherwise be unsafe.
"""
from __future__ import annotations

import pytest

from wendy.deploy_proxy import _GAME_NAME_RE


def _is_valid(name: str) -> bool:
    return bool(_GAME_NAME_RE.match(name))


@pytest.mark.parametrize("name", [
    "a",
    "1",
    "ab",
    "my-game",
    "game123",
    "a" * 32,  # 1 + 30 middle + 1 == max length
])
def test_game_name_accepts_valid(name):
    assert _is_valid(name)


@pytest.mark.parametrize("name", [
    "",            # empty
    "A",           # uppercase not allowed
    "MyGame",      # uppercase
    "-game",       # leading hyphen
    "game-",       # trailing hyphen
    "my.game",     # dot (path traversal vector)
    "my/game",     # slash (path separator)
    "my game",     # whitespace
    "my_game",     # underscore not allowed
    "../etc",      # traversal
    "a" * 33,      # too long
])
def test_game_name_rejects_invalid(name):
    assert not _is_valid(name)
