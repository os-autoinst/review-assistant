# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the review runner, with a fake runner in place of git, Hermes and review-lint."""

import json
import subprocess  # noqa: S404 - builds CompletedProcess fixtures only
from collections.abc import Sequence
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from review_assistant.config import load_config
from review_assistant.review import (
    PullRequest,
    ReviewError,
    Workspace,
    ensure_clone,
    review_pull_request,
    run_command,
    validate_review_summary,
)

EXAMPLE = Path(__file__).parent.parent / "config" / "reviewer.example.yaml"
CONFIG = load_config(EXAMPLE)
SKILL_ROOT = Path("/cache/skill/skills/openqa")
CLONE = Path("/cache/openQA")
NUMBER = 8079
HEAD = "a6d255abcbce67a380f6716c94e861869ed64882"
HOOKLESS_GIT = ["git", "-c", "core.hooksPath=/dev/null"]
PR = PullRequest(
    repo="os-autoinst/openQA",
    number=NUMBER,
    head_sha=HEAD,
    title="Fix retry handling",
    body="Retries were unbounded.",
    diff="diff --git a/lib/x.pm b/lib/x.pm\n",
    changed_lines=42,
)


SECRET = "read-token-placeholder"
DEFAULT_REVIEW = json.dumps({
    "commit_id": HEAD,
    "event": "APPROVE",
    "body": "Verified the test and retry logic in Assets.pm.",
    "comments": [],
})


class _FakeRunner:
    def __init__(
        self,
        *,
        review_text: str = DEFAULT_REVIEW,
        hermes_out: str = "",
        hermes_rc: int = 0,
        lint_rc: int = 0,
        lint_out: str = "",
        write_review: bool = True,
        head: str = HEAD,
        git_rc: int = 0,
    ) -> None:
        self.review_text = review_text
        self.hermes_out = hermes_out
        self.hermes_rc = hermes_rc
        self.lint_rc = lint_rc
        self.lint_out = lint_out
        self.write_review = write_review
        self.head = head
        self.git_rc = git_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if "clone" in argv:
            (Path(argv[-1]) / ".git").mkdir(parents=True, exist_ok=True)
        if argv[0] == "env":
            if self.write_review and self.hermes_rc == 0:
                review = Path(argv[argv.index("--in") + 1]) / f"review-{NUMBER}.json"
                review.write_text(self.review_text, encoding="utf-8")
            return subprocess.CompletedProcess(
                argv, self.hermes_rc, stdout="", stderr=self.hermes_out or "hermes failed"
            )
        if argv[0] == "python3":
            return subprocess.CompletedProcess(argv, self.lint_rc, stdout=self.lint_out, stderr="")
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{self.head}\n", stderr="")
        return subprocess.CompletedProcess(argv, self.git_rc, stdout="", stderr="git failed")

    def hermes_argv(self) -> list[str]:
        return next(call for call in self.calls if call[0] == "env")


def _review(tmp_path: Path, runner: _FakeRunner) -> Path:
    return review_pull_request(CONFIG, PR, Workspace(SKILL_ROOT, tmp_path, CLONE, (SECRET, "")), runner)


def test_missing_clone_is_cloned_shallow_once(tmp_path: Path) -> None:
    runner = _FakeRunner()
    ensure_clone("https://github.com/os-autoinst/openQA.git", tmp_path / "openQA", runner)
    assert runner.calls == [
        [
            *HOOKLESS_GIT,
            "clone",
            "--quiet",
            "--depth",
            "1",
            "https://github.com/os-autoinst/openQA.git",
            str(tmp_path / "openQA"),
        ]
    ]
    ensure_clone("https://github.com/os-autoinst/openQA.git", tmp_path / "openQA", runner)
    assert len(runner.calls) == 1


def test_failed_clone_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="git clone exited 128"):
        ensure_clone("url", tmp_path / "openQA", _FakeRunner(git_rc=128))


def test_pr_head_is_fetched_by_pull_ref_and_checked_out_detached(tmp_path: Path) -> None:
    runner = _FakeRunner()
    _review(tmp_path, runner)
    worktree = tmp_path / f"pr-{NUMBER}" / "worktree"
    assert runner.calls[0] == [
        *HOOKLESS_GIT,
        "-C",
        str(CLONE),
        "fetch",
        "--quiet",
        "--depth",
        "1",
        "origin",
        f"+refs/pull/{NUMBER}/head:refs/review-assistant/pr-{NUMBER}",
    ]
    assert runner.calls[1] == [*HOOKLESS_GIT, "-C", str(CLONE), "worktree", "prune"]
    assert runner.calls[2] == [
        *HOOKLESS_GIT,
        "-C",
        str(CLONE),
        "worktree",
        "add",
        "--detach",
        str(worktree),
        f"refs/review-assistant/pr-{NUMBER}",
    ]
    assert runner.calls[3] == [*HOOKLESS_GIT, "-C", str(worktree), "rev-parse", "HEAD"]


def test_moved_pr_head_is_rejected_before_review(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="moved: checked out"):
        _review(tmp_path, _FakeRunner(head="b" * 40))


def test_git_failure_is_rejected_before_review(tmp_path: Path) -> None:
    runner = _FakeRunner(git_rc=128)
    with pytest.raises(ReviewError, match="git fetch exited 128"):
        _review(tmp_path, runner)
    assert all("hermes" not in call for call in runner.calls)


def test_hermes_gets_only_provider_model_scratch_and_toolsets(tmp_path: Path) -> None:
    runner = _FakeRunner()
    _review(tmp_path, runner)
    argv = runner.hermes_argv()
    assert argv[0:4] == ["env", f"HERMES_WRITE_SAFE_ROOT={tmp_path / f'pr-{NUMBER}'}", "hermes", "-z"]
    assert argv[5:] == [
        "--provider",
        "google-vertex",
        "-m",
        "google/gemini-3.8-flash",
        "--in",
        str(tmp_path / f"pr-{NUMBER}"),
        "-t",
        "file",
    ]


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param("openqa-pr-review", id="playbook-named"),
        pytest.param(str(SKILL_ROOT), id="skill-root-given"),
        pytest.param(f"review-{NUMBER}.json", id="review-path-given"),
        pytest.param("Post nothing", id="no-posting-instruction"),
        pytest.param("review body must not be empty", id="summary-required"),
        pytest.param("Avoid generic filler like 'LGTM'", id="no-filler-instruction"),
        pytest.param("Set event to 'APPROVE'", id="approve-guidance"),
        pytest.param("Set event to 'COMMENT'", id="comment-guidance"),
        pytest.param("Never use 'REQUEST_CHANGES'", id="no-request-changes"),
        pytest.param("unavailable in this run", id="file-only-tools-disclosed"),
    ],
)
def test_prompt_names_playbook_and_paths(tmp_path: Path, expected: str) -> None:
    runner = _FakeRunner()
    _review(tmp_path, runner)
    assert expected in runner.hermes_argv()[4]


def test_prompt_does_not_ask_the_agent_to_run_commands(tmp_path: Path) -> None:
    runner = _FakeRunner()
    _review(tmp_path, runner)
    prompt = runner.hermes_argv()[4]
    assert "review-lint" not in prompt
    assert "run " not in prompt


def test_scratch_holds_diff_and_pr_text(tmp_path: Path) -> None:
    _review(tmp_path, _FakeRunner())
    scratch = tmp_path / f"pr-{NUMBER}"
    assert (scratch / "diff.patch").read_text(encoding="utf-8") == PR.diff
    assert (scratch / "pr.md").read_text(encoding="utf-8") == "# Fix retry handling\n\nRetries were unbounded.\n"


def test_passing_review_is_linted_in_python_with_changed_lines(tmp_path: Path) -> None:
    runner = _FakeRunner()
    review = _review(tmp_path, runner)
    assert review == tmp_path / f"pr-{NUMBER}" / f"review-{NUMBER}.json"
    assert runner.calls[-2] == [
        "python3",
        str(SKILL_ROOT / "scripts" / "review-lint.py"),
        str(review),
        "--lines",
        "42",
    ]
    assert runner.calls[-1] == [
        *HOOKLESS_GIT,
        "-C",
        str(CLONE),
        "worktree",
        "remove",
        "--force",
        str(tmp_path / f"pr-{NUMBER}" / "worktree"),
    ]


def test_repeated_review_replaces_the_previous_scratch(tmp_path: Path) -> None:
    _review(tmp_path, _FakeRunner())
    stale = tmp_path / f"pr-{NUMBER}" / "stale"
    stale.write_text("left over from an earlier head", encoding="utf-8")
    _review(tmp_path, _FakeRunner())
    assert not stale.exists()


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        pytest.param(_FakeRunner(hermes_rc=3), "hermes exited 3: hermes failed", id="hermes-fails"),
        pytest.param(_FakeRunner(write_review=False), "hermes wrote no review", id="no-review-written"),
        pytest.param(
            _FakeRunner(lint_rc=1, lint_out="too many nits"), "review-lint exited 1: too many nits", id="lint-findings"
        ),
        pytest.param(
            _FakeRunner(lint_rc=2, lint_out="not a review"), "review-lint exited 2: not a review", id="lint-usage-error"
        ),
    ],
)
def test_failed_review_is_rejected(tmp_path: Path, runner: _FakeRunner, message: str) -> None:
    with pytest.raises(ReviewError, match=message):
        _review(tmp_path, runner)


@pytest.mark.parametrize(
    "review_text",
    [
        pytest.param(f'{{"body": "{SECRET}"}}', id="known-token"),
        pytest.param('{"comments": [{"body": "ghp_' + "a" * 36 + '"}]}', id="github-token-pattern"),
    ],
)
def test_review_with_a_credential_is_rejected_before_lint(tmp_path: Path, review_text: str) -> None:
    runner = _FakeRunner(review_text=review_text)
    with pytest.raises(ReviewError, match="contains a credential"):
        _review(tmp_path, runner)
    assert all(call[0] != "python3" for call in runner.calls)


@pytest.mark.parametrize(
    "runner",
    [
        pytest.param(_FakeRunner(hermes_rc=1, hermes_out=f"my token is {SECRET}"), id="hermes-reply"),
        pytest.param(_FakeRunner(lint_rc=1, lint_out=f"line 3: {SECRET}"), id="lint-finding"),
    ],
)
def test_agent_output_in_errors_is_redacted(tmp_path: Path, runner: _FakeRunner) -> None:
    with pytest.raises(ReviewError, match=r"\[redacted\]") as error:
        _review(tmp_path, runner)
    assert SECRET not in str(error.value)


def test_stale_review_from_earlier_run_is_not_accepted(tmp_path: Path) -> None:
    scratch = tmp_path / f"pr-{NUMBER}"
    scratch.mkdir()
    (scratch / f"review-{NUMBER}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ReviewError, match="hermes wrote no review"):
        _review(tmp_path, _FakeRunner(write_review=False))


def test_run_command_executes_argv_without_shell(mocker: MockerFixture) -> None:
    completed = subprocess.CompletedProcess(["hermes", "-z"], 0, stdout="OK", stderr="")
    run = mocker.patch("review_assistant.review.subprocess.run", return_value=completed)
    assert run_command(["hermes", "-z"]) is completed
    run.assert_called_once_with(["hermes", "-z"], check=False, capture_output=True, text=True)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("Verified retry handling in Assets.pm.", id="simple-verified"),
        pytest.param("Checked Screenshot.pm; verified filter ordering.", id="multi-clause"),
        pytest.param("LGTM, verified the test harness and module syntax.", id="filler-with-substance"),
    ],
)
def test_validate_review_summary_accepts_valid_summary(body: str) -> None:
    validate_review_summary(body)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param("", "review body must not be empty", id="empty"),
        pytest.param("   \n\t  ", "review body must not be empty", id="whitespace"),
        pytest.param("LGTM", "generic filler", id="lgtm-upper"),
        pytest.param("lgtm!", "generic filler", id="lgtm-punct"),
        pytest.param("Looks good to me.", "generic filler", id="looks-good-to-me"),
        pytest.param("looks good", "generic filler", id="looks-good"),
        pytest.param("+1", "generic filler", id="plus-one"),
        pytest.param("Approved.", "generic filler", id="approved"),
        pytest.param("All good!", "generic filler", id="all-good"),
        pytest.param("done", "generic filler", id="done"),
        pytest.param("ok", "generic filler", id="ok"),
    ],
)
def test_validate_review_summary_rejects_empty_and_filler(body: str, message: str) -> None:
    with pytest.raises(ReviewError, match=message):
        validate_review_summary(body)


@pytest.mark.parametrize(
    ("review_text", "message"),
    [
        pytest.param("not json at all", "is not valid JSON", id="not-json"),
        pytest.param('["not", "a", "dict"]', "must be a JSON object", id="not-dict"),
        pytest.param(
            json.dumps({"commit_id": HEAD, "event": "APPROVE"}),
            "must be a JSON object with a string 'body'",
            id="missing-body",
        ),
        pytest.param(
            json.dumps({"commit_id": HEAD, "event": "APPROVE", "body": 123}),
            "must be a JSON object with a string 'body'",
            id="non-string-body",
        ),
        pytest.param(
            json.dumps({"commit_id": HEAD, "event": "APPROVE", "body": ""}),
            "review body must not be empty",
            id="empty-body",
        ),
        pytest.param(
            json.dumps({"commit_id": HEAD, "event": "APPROVE", "body": "LGTM"}),
            "review body contains generic filler",
            id="filler-body",
        ),
    ],
)
def test_review_with_invalid_body_is_rejected(tmp_path: Path, review_text: str, message: str) -> None:
    runner = _FakeRunner(review_text=review_text)
    with pytest.raises(ReviewError, match=message):
        _review(tmp_path, runner)


def test_approve_with_body_ignores_review_lint_approval_text_finding(tmp_path: Path) -> None:
    lint_out = (
        "budget: 42 changed lines, lib: 4 non-blocking items, 1000 chars\n"
        "draft: APPROVE, 0 comments + body; blocking 0, nit 0, non-blocking prose 45 chars\n"
        "findings: 1\n"
        "  F event review: an approval carries no text; findings go in a COMMENT review\n"
    )
    runner = _FakeRunner(lint_rc=1, lint_out=lint_out)
    review = _review(tmp_path, runner)
    assert review == tmp_path / f"pr-{NUMBER}" / f"review-{NUMBER}.json"


def test_approve_with_body_and_other_findings_fails_lint(tmp_path: Path) -> None:
    lint_out = (
        "budget: 42 changed lines, lib: 4 non-blocking items, 1000 chars\n"
        "draft: APPROVE, 0 comments + body; blocking 0, nit 0, non-blocking prose 45 chars\n"
        "findings: 2\n"
        "  F bare-id body: '1234567' is a bare number; write each job as its full URL\n"
        "  F event review: an approval carries no text; findings go in a COMMENT review\n"
    )
    runner = _FakeRunner(lint_rc=1, lint_out=lint_out)
    with pytest.raises(ReviewError, match=r"(?s)review-lint exited 1:.*bare-id body"):
        _review(tmp_path, runner)
