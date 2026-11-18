# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Reviewer configuration loaded from YAML; never carries the GitHub token."""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_TOKEN_PATTERN = re.compile(r"github_pat_\w+|gh[pousr]_[A-Za-z0-9]{20,}")


class ConfigError(RuntimeError):
    """The configuration file is missing, unreadable or invalid."""


class SkillConfig(BaseModel):
    """Pinned openQA-skill checkout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str
    sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")


class ReviewerConfig(BaseModel):
    """Operator-editable reviewer settings. Contains no credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_name: str
    skill: SkillConfig
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    toolsets: str = Field(min_length=1)
    pilot_cap: int = Field(default=10, ge=1)


def _find_token_like(value: object) -> bool:
    match value:
        case str():
            return _TOKEN_PATTERN.search(value) is not None
        case dict():
            return any(_find_token_like(item) for item in value.values())
        case list():
            return any(_find_token_like(item) for item in value)
        case _:
            return False


def parse_config(data: object) -> ReviewerConfig:
    """Validate raw YAML data, refusing any token-like value."""
    if _find_token_like(data):
        msg = "configuration contains a token-like value; set REVIEW_ASSISTANT_GH_TOKEN in the environment instead"
        raise ValueError(msg)
    return ReviewerConfig.model_validate(data)


def load_config(path: Path) -> ReviewerConfig:
    """Load and validate the reviewer configuration file at path, reporting failures as ConfigError."""
    try:
        return parse_config(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError) as error:
        msg = f"configuration {path} is invalid: {error}"
        raise ConfigError(msg) from None
