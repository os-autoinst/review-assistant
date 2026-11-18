# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the review CLI, with GitHub, the skill checkout and Hermes replaced by fakes."""

import json
import os
import signal
import subprocess  # noqa: S404 - builds CompletedProcess fixtures only
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from pytest_mock import MockerFixture

from review_assistant.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from review_assistant.config import ReviewerConfig
from review_assistant.github import PollResult, PullSummary, ReviewDraft
from review_assistant.hardening import HardeningError
from review_assistant.review import PullRequest, ReviewError, Workspace
from review_assistant.skill import SkillCheckoutError
from review_assistant.state import ReviewRecord, load_state, save_state, state_key

REPO = "os-autoinst/openQA"
REPO_URL = "https://github.com/os-autoinst/openQA.git"
EXAMPLE = Path(__file__).parent.parent / "config" / "reviewer.example.yaml"
READ_TOKEN = "read-token-placeholder"
WRITE_TOKEN = "write-token-placeholder"
PULL_NUMBERS = (1, 2, 3)


def _head(number: int) -> str:
    return str(number) * 40


PULLS = [
    PullSummary(number=n, head_sha=_head(n), title=f"PR {n}", body="", draft=False, author="contributor")
    for n in PULL_NUMBERS
]


@pytest.fixture
def fakes(mocker: MockerFixture, tmp_path: Path) -> SimpleNamespace:
    mocker.patch.dict(os.environ, {"REVIEW_ASSISTANT_GH_READ_TOKEN": READ_TOKEN})
    ns = SimpleNamespace(
        state_file=tmp_path / "state.json",
        skill_cache=tmp_path / "skills",
        clone=tmp_path / "openQA",
        review_errors={},
        review_overrides={},
        post_errors={},
    )

    def fetch(_token: str, repo: str, number: int) -> PullRequest:
        return PullRequest(
            repo=repo,
            number=number,
            head_sha=_head(number),
            title=f"PR {number}",
            body="",
            diff="diff --git a/x b/x\n",
            changed_lines=3,
        )

    def draft(_config: ReviewerConfig, pull: PullRequest, workspace: Workspace) -> Path:
        if pull.number in ns.review_errors:
            raise ns.review_errors[pull.number]
        directory = workspace.scratch_root / f"pr-{pull.number}"
        directory.mkdir(parents=True, exist_ok=True)
        review = {
            "commit_id": pull.head_sha,
            "event": "COMMENT",
            "body": f"review {pull.number}",
            "comments": [],
            **ns.review_overrides.get(pull.number, {}),
        }
        path = directory / f"review-{pull.number}.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        return path

    def post(_token: str, _repo: str, number: int, review: dict) -> int:
        if number in ns.post_errors:
            raise ns.post_errors[number]
        ReviewDraft.model_validate(review)
        return 5000 + number

    mocker.patch("review_assistant.cli.make_non_dumpable")
    ns.checkout = mocker.patch("review_assistant.cli.checkout_skill", return_value=tmp_path / "checkout")
    ns.listing = mocker.patch("review_assistant.cli.list_open_pulls", return_value=PULLS)
    ns.fetch = mocker.patch("review_assistant.cli.fetch_pull_request", side_effect=fetch)
    ns.review = mocker.patch("review_assistant.cli.review_pull_request", side_effect=draft)
    ns.post_review = mocker.patch("review_assistant.cli.post_review", side_effect=post)
    ns.ensure_clone = mocker.patch("review_assistant.cli.ensure_clone")
    return ns


def _argv(ns: SimpleNamespace, *extra: str) -> list[str]:
    return [
        "run",
        "--config",
        str(EXAMPLE),
        "--state-file",
        str(ns.state_file),
        "--skill-cache",
        str(ns.skill_cache),
        "--clone",
        str(ns.clone),
        *extra,
    ]


def _with_write_token(mocker: MockerFixture) -> None:
    mocker.patch.dict(os.environ, {"REVIEW_ASSISTANT_GH_TOKEN": WRITE_TOKEN})


def _fetched_numbers(ns: SimpleNamespace) -> list[int]:
    return [call.args[2] for call in ns.fetch.call_args_list]


def test_dry_run_prints_reviews_and_writes_nothing(fakes: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_argv(fakes)) == EXIT_OK
    out = capsys.readouterr().out
    assert f"# {REPO}#1 at {_head(1)}" in out
    assert '"body": "review 3"' in out
    fakes.post_review.assert_not_called()
    assert not fakes.state_file.exists()


def test_dry_run_rejects_a_malformed_review(fakes: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    fakes.listing.return_value = [PULLS[0]]
    fakes.review_overrides.update({1: {"event": "REQUEST_CHANGES"}})
    assert main(_argv(fakes)) == EXIT_FAILED
    assert not capsys.readouterr().out


def test_missing_config_is_reported_without_a_traceback(
    fakes: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _argv(fakes)
    argv[argv.index("--config") + 1] = str(tmp_path / "absent.yaml")
    assert main(argv) == EXIT_USAGE
    assert "configuration" in capsys.readouterr().err


def test_skill_checkout_failure_is_reported_without_a_traceback(
    fakes: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.checkout.side_effect = SkillCheckoutError("clone failed")
    assert main(_argv(fakes)) == EXIT_USAGE
    assert "clone failed" in capsys.readouterr().err


def test_post_records_each_posted_review(fakes: SimpleNamespace, mocker: MockerFixture) -> None:
    _with_write_token(mocker)
    assert main(_argv(fakes, "--post")) == EXIT_OK
    assert fakes.post_review.call_args_list[0].args[:3] == (WRITE_TOKEN, REPO, 1)
    assert load_state(fakes.state_file) == {
        state_key(REPO, n): ReviewRecord(head_sha=_head(n), review_id=5000 + n) for n in PULL_NUMBERS
    }


def test_post_without_write_token_fails_before_listing(fakes: SimpleNamespace, mocker: MockerFixture) -> None:
    mocker.patch.dict(os.environ, {}, clear=True)
    assert main(_argv(fakes, "--post")) == EXIT_USAGE
    fakes.listing.assert_not_called()


def test_dry_run_removes_both_tokens_so_agent_cannot_inherit_them(
    fakes: SimpleNamespace, mocker: MockerFixture
) -> None:
    _with_write_token(mocker)
    assert main(_argv(fakes)) == EXIT_OK
    assert "REVIEW_ASSISTANT_GH_TOKEN" not in os.environ
    assert "REVIEW_ASSISTANT_GH_READ_TOKEN" not in os.environ
    assert fakes.listing.call_args.args[0] == READ_TOKEN
    fakes.post_review.assert_not_called()


def test_missing_read_token_fails_before_skill_checkout_and_listing(
    fakes: SimpleNamespace, mocker: MockerFixture
) -> None:
    mocker.patch.dict(os.environ, {}, clear=True)
    assert main(_argv(fakes)) == EXIT_USAGE
    fakes.checkout.assert_not_called()
    fakes.listing.assert_not_called()


def test_reviewed_head_is_skipped(fakes: SimpleNamespace) -> None:
    save_state(fakes.state_file, {state_key(REPO, 1): ReviewRecord(head_sha=_head(1), review_id=1)})
    assert main(_argv(fakes)) == EXIT_OK
    assert _fetched_numbers(fakes) == [2, 3]


def test_limit_caps_new_reviews_after_skips(fakes: SimpleNamespace) -> None:
    save_state(fakes.state_file, {state_key(REPO, 1): ReviewRecord(head_sha=_head(1), review_id=1)})
    assert main(_argv(fakes, "--limit", "1")) == EXIT_OK
    assert _fetched_numbers(fakes) == [2]


def test_draft_and_bot_authored_pulls_are_skipped(fakes: SimpleNamespace) -> None:
    draft = PullSummary(number=1, head_sha=_head(1), title="D", body="", draft=True, author="contributor")
    bot = PullSummary(number=2, head_sha=_head(2), title="B", body="", draft=False, author="os-autoinst-review")
    normal = PullSummary(number=3, head_sha=_head(3), title="N", body="", draft=False, author="contributor")
    fakes.listing.return_value = [draft, bot, normal]
    assert main(_argv(fakes)) == EXIT_OK
    assert _fetched_numbers(fakes) == [3]


@pytest.mark.parametrize(
    "configure",
    [
        pytest.param(lambda ns: ns.review_errors.update({1: ReviewError("lint failed")}), id="review-raises"),
        pytest.param(lambda ns: ns.review_overrides.update({1: {"commit_id": "0" * 40}}), id="commit-id-mismatch"),
        pytest.param(
            lambda ns: ns.review_overrides.update({1: {"event": "REQUEST_CHANGES"}}), id="request-changes-event"
        ),
        pytest.param(lambda ns: ns.post_errors.update({1: requests.HTTPError("403")}), id="post-http-error"),
    ],
)
def test_failed_pull_is_logged_and_not_recorded_while_run_continues(
    fakes: SimpleNamespace,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    configure: Callable[[SimpleNamespace], None],
) -> None:
    _with_write_token(mocker)
    configure(fakes)
    assert main(_argv(fakes, "--post")) == EXIT_FAILED
    assert set(load_state(fakes.state_file)) == {state_key(REPO, 2), state_key(REPO, 3)}
    assert f"{REPO}#1 not recorded" in caplog.text


@pytest.mark.parametrize(
    ("post", "secrets"),
    [
        pytest.param(True, (READ_TOKEN, WRITE_TOKEN), id="post-guards-both-tokens"),
        pytest.param(False, (READ_TOKEN, ""), id="dry-run-guards-read-token"),
    ],
)
def test_review_is_told_which_tokens_must_never_appear(
    fakes: SimpleNamespace, mocker: MockerFixture, *, post: bool, secrets: tuple[str, str]
) -> None:
    _with_write_token(mocker)
    main(_argv(fakes, *(["--post"] if post else [])))
    assert fakes.review.call_args.args[2].secrets == secrets


def _check_argv(ns: SimpleNamespace) -> list[str]:
    return ["check-isolation", *_argv(ns)[1:]]


@pytest.fixture
def isolation_script(mocker: MockerFixture, tmp_path: Path) -> Path:
    script = tmp_path / "check-isolation.sh"
    script.touch()
    mocker.patch("review_assistant.cli.ISOLATION_CHECK", script)
    return script


def test_check_isolation_runs_script_as_the_agent_would_run(
    fakes: SimpleNamespace, mocker: MockerFixture, isolation_script: Path
) -> None:
    _with_write_token(mocker)
    environ_at_run: list[dict[str, str]] = []

    def script(argv: list[str], **_kwargs: bool) -> subprocess.CompletedProcess[str]:
        environ_at_run.append(dict(os.environ))
        return subprocess.CompletedProcess(argv, 3)

    run = mocker.patch("review_assistant.cli.subprocess.run", side_effect=script)
    assert main(_check_argv(fakes)) == 3
    argv = run.call_args.args[0]
    scratch = argv[-1]
    assert argv == [
        "env",
        f"HERMES_WRITE_SAFE_ROOT={scratch}",
        str(isolation_script),
        str(fakes.state_file),
        str(fakes.clone),
        str(fakes.checkout.return_value),
        scratch,
    ]
    assert not {"REVIEW_ASSISTANT_GH_TOKEN", "REVIEW_ASSISTANT_GH_READ_TOKEN"} & set(environ_at_run[0])
    fakes.listing.assert_not_called()


@pytest.mark.usefixtures("isolation_script")
def test_check_isolation_needs_the_write_token_to_prove_its_removal(
    fakes: SimpleNamespace, mocker: MockerFixture
) -> None:
    mocker.patch.dict(os.environ, {"REVIEW_ASSISTANT_GH_READ_TOKEN": READ_TOKEN}, clear=True)
    run = mocker.patch("review_assistant.cli.subprocess.run")
    assert main(_check_argv(fakes)) == EXIT_USAGE
    run.assert_not_called()


def test_check_isolation_outside_the_image_fails_cleanly(
    fakes: SimpleNamespace, mocker: MockerFixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_write_token(mocker)
    mocker.patch("review_assistant.cli.ISOLATION_CHECK", tmp_path / "absent.sh")
    run = mocker.patch("review_assistant.cli.subprocess.run")
    assert main(_check_argv(fakes)) == EXIT_USAGE
    assert "runs inside the image" in capsys.readouterr().err
    run.assert_not_called()


def test_failed_hardening_stops_before_any_listing(fakes: SimpleNamespace, mocker: MockerFixture) -> None:
    mocker.patch("review_assistant.cli.make_non_dumpable", side_effect=HardeningError("prctl refused"))
    assert main(_argv(fakes)) == EXIT_USAGE
    fakes.listing.assert_not_called()


def test_given_clone_is_reused_and_not_cloned(fakes: SimpleNamespace) -> None:
    main(_argv(fakes))
    fakes.ensure_clone.assert_called_once_with(REPO_URL, fakes.clone)


def test_absent_clone_is_cloned_into_the_scratch_root(fakes: SimpleNamespace) -> None:
    argv = [arg for arg in _argv(fakes) if arg not in {"--clone", str(fakes.clone)}]
    assert main(argv) == EXIT_OK
    url, clone = fakes.ensure_clone.call_args.args
    assert url == REPO_URL
    assert clone.parent.name.startswith("review-assistant-")


def _daemon_argv(ns: SimpleNamespace, heartbeat: Path, *extra: str) -> list[str]:
    return ["daemon", *_argv(ns, "--heartbeat-file", str(heartbeat), "--poll-interval", "0", *extra)[1:]]


def test_daemon_sweeps_then_stops_between_reviews_on_sigterm(
    fakes: SimpleNamespace, mocker: MockerFixture, tmp_path: Path
) -> None:
    _with_write_token(mocker)
    signal_mock = mocker.patch("review_assistant.cli.signal.signal")
    polls: list[str | None] = []

    def poll(_token: str, _repo: str, etag: str | None) -> PollResult:
        polls.append(etag)
        if len(polls) == 1:
            return PollResult(pulls=PULLS, etag="v1", remaining=5000, reset=None)
        signal_mock.call_args_list[0].args[1](signal.SIGTERM, None)
        return PollResult(pulls=None, etag="v1", remaining=5000, reset=None)

    mocker.patch("review_assistant.cli.poll_open_pulls", side_effect=poll)
    assert main(_daemon_argv(fakes, tmp_path / "heartbeat", "--post")) == EXIT_OK
    assert polls == [None, "v1"]
    assert [call.args[2] for call in fakes.post_review.call_args_list] == list(PULL_NUMBERS)
    assert set(load_state(fakes.state_file)) == {state_key(REPO, n) for n in PULL_NUMBERS}
    assert {call.args[0] for call in signal_mock.call_args_list} == {signal.SIGTERM, signal.SIGINT}
    assert (tmp_path / "heartbeat").exists()
