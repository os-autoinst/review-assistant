# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the commit message gate that rejects cleanup commits before merge."""

import shlex
import subprocess  # noqa: S404 - runs git and gitlint in a scratch repository
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
GIT = [
    "git",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "user.email=review@example.com",
    "-c",
    "user.name=Reviewer",
]
CI_RANGE = "origin/${{ github.base_ref }}..HEAD"
CLEANUP_OPTIONS = (
    "general.ignore-fixup-commits=false",
    "general.ignore-squash-commits=false",
    "general.ignore-fixup-amend-commits=false",
)


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - every argument is built here
        command, cwd=cwd, capture_output=True, text=True, check=False
    )


def _commit(repo: Path, title: str) -> None:
    result = _run([*GIT, "commit", "--allow-empty", "-m", title], repo)
    assert result.returncode == 0, result.stderr


def _step_run(name: str) -> str:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["lint-commits"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == name)


def _ci_tokens(commit_range: str) -> list[str]:
    script = _step_run("Check commit messages").replace("\\\n", " ")
    return shlex.split(script.replace(CI_RANGE, commit_range))


def _lint_last_commit(tmp_path: Path, title: str, *options: str) -> subprocess.CompletedProcess[str]:
    assert _run([*GIT, "init", "-b", "main"], tmp_path).returncode == 0
    _commit(tmp_path, "feat: add x")
    _commit(tmp_path, title)
    return _run([sys.executable, "-m", "gitlint.cli", "--config", str(REPO / ".gitlint"), *options], tmp_path)


def test_ci_invocation_disables_the_cleanup_skips() -> None:
    tokens = _ci_tokens("main..HEAD")
    assert tokens[:3] == ["gitlint", "--commits", "main..HEAD"]
    assert tuple(tokens[3:]) == tuple(flag for option in CLEANUP_OPTIONS for flag in ("-c", option))


def test_gitlint_pin_matches_the_commit_msg_hook() -> None:
    version = _step_run("Install gitlint").rpartition("==")[2]
    hooks = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())["repos"]
    assert version == next(repo["rev"] for repo in hooks if "gitlint" in repo["repo"]).removeprefix("v")


@pytest.mark.parametrize(
    ("title", "rejected"),
    [
        pytest.param("feat: add y", False, id="conventional-title-passes"),
        pytest.param("fixup! feat: add x", True, id="fixup-rejected"),
        pytest.param("squash! feat: add x", True, id="squash-rejected"),
        pytest.param("amend! feat: add x", True, id="amend-rejected"),
    ],
)
def test_cleanup_commits_are_rejected(tmp_path: Path, title: str, *, rejected: bool) -> None:
    result = _lint_last_commit(tmp_path, title, *_ci_tokens("HEAD~1..HEAD")[1:])
    assert (result.returncode != 0) is rejected
    assert ("CC2" in result.stderr) is rejected


def test_commit_msg_hook_still_accepts_cleanup_commits(tmp_path: Path) -> None:
    result = _lint_last_commit(tmp_path, "fixup! feat: add x", "--commits", "HEAD~1..HEAD")
    assert result.returncode == 0, result.stderr
