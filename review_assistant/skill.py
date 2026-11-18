# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Pinned openQA-skill checkout used as the review instructions."""

import subprocess  # noqa: S404 - git is an external interface, argv only, no shell
from pathlib import Path

from review_assistant.config import SkillConfig
from review_assistant.review import GIT


class SkillCheckoutError(RuntimeError):
    """The checked-out skill does not match the pinned commit."""


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            [*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or str(error)
        msg = f"git {args[0]} failed while checking out the skill: {detail}"
        raise SkillCheckoutError(msg) from None
    return result.stdout.strip()


def checkout_skill(skill: SkillConfig, cache_root: Path) -> Path:
    """Return the skill under cache_root at the pinned commit, cloning or refreshing it only when not already there.

    A checkout already at the pin is used as is, so the copy baked into the read-only image needs no write.
    """
    target = cache_root / skill.sha
    if (target / ".git").is_dir():
        if _git("rev-parse", "HEAD", cwd=target).startswith(skill.sha):
            return target
        _git("fetch", "--quiet", "origin", cwd=target)
    else:
        _git("clone", "--quiet", skill.repo, str(target))
    _git("checkout", "--quiet", "--detach", skill.sha, cwd=target)
    head = _git("rev-parse", "HEAD", cwd=target)
    if not head.startswith(skill.sha):
        msg = f"openQA-skill HEAD {head} does not match pinned {skill.sha}"
        raise SkillCheckoutError(msg)
    return target
