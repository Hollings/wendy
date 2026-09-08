"""Tests for wendy.config."""
from __future__ import annotations

import os
from unittest import mock

from wendy.config import (
    MODEL_MAP,
    parse_channel_configs,
    resolve_model,
)


def test_model_map_keys():
    assert "opus" in MODEL_MAP
    assert "sonnet" in MODEL_MAP
    assert "haiku" in MODEL_MAP


def test_resolve_model_shorthand():
    # opus/sonnet/haiku pass the bare CLI alias through so the CLI picks the
    # newest release of the family; fable is deliberately pinned to an ID.
    assert resolve_model("opus") == "opus"
    assert resolve_model("sonnet") == "sonnet"
    assert resolve_model("haiku") == "haiku"
    assert resolve_model("fable").startswith("claude-fable-")


def test_resolve_model_env_override():
    with mock.patch.dict(os.environ, {"WENDY_MODEL_OVERRIDE": "opus"}, clear=False):
        assert resolve_model("sonnet") == "opus"
        assert resolve_model(None) == "opus"
        # The overload-retry path opts out of the override.
        assert resolve_model("sonnet", allow_env_override=False) == "sonnet"
    with mock.patch.dict(os.environ, {"WENDY_MODEL_OVERRIDE": "claude-opus-5"}, clear=False):
        assert resolve_model(None) == "claude-opus-5"


def test_resolve_model_none_defaults_to_sonnet():
    assert resolve_model(None) == MODEL_MAP["sonnet"]


def test_resolve_model_passthrough():
    assert resolve_model("claude-custom-model") == "claude-custom-model"


def test_parse_channel_configs_empty():
    with mock.patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": ""}, clear=False):
        configs = parse_channel_configs()
        assert configs == {}


def test_parse_channel_configs_valid():
    config_json = '[{"id": "123", "name": "test", "mode": "chat"}]'
    with mock.patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": config_json}, clear=False):
        configs = parse_channel_configs()
        assert 123 in configs
        assert configs[123]["name"] == "test"
        assert configs[123]["mode"] == "chat"
        assert configs[123]["_folder"] == "test"


def test_parse_channel_configs_with_folder():
    config_json = '[{"id": "456", "name": "coding", "mode": "full", "folder": "dev"}]'
    with mock.patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": config_json}, clear=False):
        configs = parse_channel_configs()
        assert configs[456]["_folder"] == "dev"


def test_parse_channel_configs_invalid_json():
    with mock.patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": "not json"}, clear=False):
        configs = parse_channel_configs()
        assert configs == {}


def test_parse_channel_configs_missing_fields():
    config_json = '[{"id": "123"}]'  # missing name
    with mock.patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": config_json}, clear=False):
        configs = parse_channel_configs()
        assert configs == {}
