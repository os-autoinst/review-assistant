# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the pinned openQA-skill checkout."""

import subprocess  # noqa: S404 - builds CompletedProcess and CalledProcessError fixtures only
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from review_assistant.config import SkillConfig
from review_assistant.skill import SkillCheckoutError, checkout_skill

SHA = "a6d255abcbce67a380f6716c94e861869ed64882"
REPO = "https://github.com/os-autoinst/openQA-skill.git"
SKILL = SkillConfig(repo=REPO, sha=SHA)
HOOKLESS_GIT = ["git", "-c", "core.hooksPath=/dev/null"]


def _mock_git(mocker: MockerFixture, head: str) -> MagicMock:
    return mocker.patch(
        "review_assistant.skill.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{head}\n"),
    )


def _argv(run: MagicMock) -> list[list[str]]:
    return [call.args[0] for call in run.call_args_list]


def test_fresh_checkout_clones_then_detaches_pinned_sha(mocker: MockerFixture, tmp_path: Path) -> None:
    run = _mock_git(mocker, head=SHA)
    target = checkout_skill(SKILL, tmp_path)
    assert target == tmp_path / SHA
    assert _argv(run) == [
        [*HOOKLESS_GIT, "clone", "--quiet", REPO, str(target)],
        [*HOOKLESS_GIT, "checkout", "--quiet", "--detach", SHA],
        [*HOOKLESS_GIT, "rev-parse", "HEAD"],
    ]


def test_existing_checkout_at_pin_is_used_without_fetch_or_write(mocker: MockerFixture, tmp_path: Path) -> None:
    (tmp_path / SHA / ".git").mkdir(parents=True)
    run = _mock_git(mocker, head=SHA)
    assert checkout_skill(SKILL, tmp_path) == tmp_path / SHA
    assert _argv(run) == [[*HOOKLESS_GIT, "rev-parse", "HEAD"]]


def test_existing_checkout_elsewhere_is_fetched_instead_of_cloned(mocker: MockerFixture, tmp_path: Path) -> None:
    (tmp_path / SHA / ".git").mkdir(parents=True)
    run = mocker.patch(
        "review_assistant.skill.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{'0' * 40}\n"),
            *[subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{SHA}\n")] * 3,
        ],
    )
    checkout_skill(SKILL, tmp_path)
    assert _argv(run) == [
        [*HOOKLESS_GIT, "rev-parse", "HEAD"],
        [*HOOKLESS_GIT, "fetch", "--quiet", "origin"],
        [*HOOKLESS_GIT, "checkout", "--quiet", "--detach", SHA],
        [*HOOKLESS_GIT, "rev-parse", "HEAD"],
    ]


@pytest.mark.parametrize(
    "head",
    [
        pytest.param("0" * 40, id="other-commit"),
        pytest.param("a6d255abcbce0000000000000000000000000000", id="same-prefix-different-commit"),
    ],
)
def test_head_mismatch_is_rejected(mocker: MockerFixture, tmp_path: Path, head: str) -> None:
    _mock_git(mocker, head=head)
    with pytest.raises(SkillCheckoutError, match="does not match pinned"):
        checkout_skill(SKILL, tmp_path)


def test_failing_git_is_reported_as_a_skill_checkout_error(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch(
        "review_assistant.skill.subprocess.run",
        side_effect=subprocess.CalledProcessError(128, [*HOOKLESS_GIT, "clone"], stderr="boom"),
    )
    with pytest.raises(SkillCheckoutError, match="failed"):
        checkout_skill(SKILL, tmp_path)
