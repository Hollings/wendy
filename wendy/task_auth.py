"""Short-lived, scoped API capabilities for controller sessions and workers.

Tokens exist only in the bot and the appropriate child environment. They are
never persisted in reports, task listings or prompts.
"""
from __future__ import annotations

import secrets

_tokens: dict[str, dict] = {}


def issue(**scope) -> str:
    token = secrets.token_urlsafe(32)
    _tokens[token] = scope
    return token


def lookup(token: str) -> dict | None:
    scope = _tokens.get(token)
    return scope if scope and scope.get('active', True) else None


def revoke(token: str):
    _tokens.pop(token, None)
