# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for reviewer configuration parsing."""

from pathlib import Path

import pydantic
import pytest

from review_assistant.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).parent.parent / "config" / "reviewer.example.yaml"
SKILL = {"repo": "https://github.com/os-autoinst/openQA-skill.git", "sha": "a6d255abcbce"}

VALID = {
    "agent_name": "os-autoinst-review",
    "skill": SKILL,
    "provider": "google-vertex",
    "model": "google/gemini-3.8-flash",
    "toolsets": "file",
}


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"pilot_cap": 0}, id="pilot-cap-zero"),
        pytest.param({"skill": {"repo": "x", "sha": "main"}}, id="skill-sha-not-hex"),
        pytest.param({"approve": True}, id="unknown-top-level-key"),
        pytest.param({"token_env": "OTHER"}, id="token-env-not-configurable"),
        pytest.param({"skill": {**SKILL, "ref": "main"}}, id="unknown-skill-key"),
        pytest.param({"agent_name": None}, id="agent-name-null"),
        pytest.param({"model": ""}, id="model-empty"),
        pytest.param({"toolsets": ""}, id="toolsets-empty"),
        pytest.param({"provider": None}, id="provider-null"),
    ],
)
def test_invalid_config_is_rejected(override: dict) -> None:
    with pytest.raises(pydantic.ValidationError):
        parse_config({**VALID, **override})


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param("github_pat_11ABCDEFG0123456789_abcdefghijklmnop", id="fine-grained-token"),
        pytest.param("ghp_abcdefghijklmnopqrstuvwxyz0123456789", id="classic-token"),
    ],
)
def test_token_like_value_is_rejected(secret: str) -> None:
    with pytest.raises(ValueError, match="token-like value"):
        parse_config({**VALID, "agent_name": secret})


def test_token_like_value_nested_in_list_is_rejected() -> None:
    nested = {**SKILL, "sha": "a6d255abcbce", "mirrors": ["ghs_abcdefghijklmnopqrstuvwxyz"]}
    with pytest.raises(ValueError, match="token-like value"):
        parse_config({**VALID, "skill": nested})


def test_example_config_loads_with_pilot_settings() -> None:
    config = load_config(EXAMPLE)
    assert (config.provider, config.model, config.toolsets, config.pilot_cap) == (
        "google-vertex",
        "google/gemini-3.8-flash",
        "file",
        10,
    )


def test_pilot_cap_defaults_to_ten() -> None:
    assert parse_config(VALID).pilot_cap == 10


@pytest.mark.parametrize(
    ("content", "test_id"),
    [
        pytest.param(None, "missing-file", id="missing-file"),
        pytest.param("agent_name: [unclosed", "malformed-yaml", id="malformed-yaml"),
        pytest.param("agent_name: reviewer\n", "schema-violation", id="schema-violation"),
        pytest.param("agent_name: ghp_" + "a" * 36 + "\n", "token-like", id="token-like-value"),
    ],
)
def test_unusable_config_is_a_config_error(tmp_path: Path, content: str | None, test_id: str) -> None:
    path = tmp_path / f"{test_id}.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="is invalid"):
        load_config(path)
