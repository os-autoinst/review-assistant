# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the continuous review loop, with scripted GitHub listings and fake reviews; no network, no sleeping."""

import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from review_assistant.daemon import RATE_LIMIT_FLOOR, RETRY_COOLDOWN_SECONDS, Daemon
from review_assistant.github import PollResult, PullSummary
from review_assistant.review import ReviewError

AGENT = "os-autoinst-review"
POLL_INTERVAL = 60
NOT_MODIFIED = PollResult(pulls=None, etag="v1", remaining=5000, reset=None)


def _sha(number: int, generation: int = 0) -> str:
    return f"{number}{generation}" * 20


def _pull(number: int, *, generation: int = 0, draft: bool = False, author: str = "contributor") -> PullSummary:
    return PullSummary(
        number=number, head_sha=_sha(number, generation), title=f"PR {number}", body="", draft=draft, author=author
    )


def _listing(*pulls: PullSummary, etag: str = "v1", remaining: int = 5000, reset: int | None = None) -> PollResult:
    return PollResult(pulls=list(pulls), etag=etag, remaining=remaining, reset=reset)


def _daemon(
    tmp_path: Path,
    results: list[PollResult | Exception],
    *,
    clock: Callable[[], float] = lambda: 0.0,
    recorded: set[int] | None = None,
    failures: dict[int, Exception] | None = None,
    stop_after: set[int] | None = None,
    limit: int = 1000,
) -> SimpleNamespace:
    queue = list(results)
    etags: list[str | None] = []
    attempts: list[tuple[int, str]] = []
    reviewed: list[tuple[int, str]] = []
    failing = failures if failures is not None else {}
    stop_event = threading.Event()
    stopping = stop_after if stop_after is not None else set()

    def poll(etag: str | None) -> PollResult:
        etags.append(etag)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def review(pull: PullSummary) -> None:
        attempts.append((pull.number, pull.head_sha))
        if pull.number in failing:
            raise failing[pull.number]
        reviewed.append((pull.number, pull.head_sha))
        if pull.number in stopping:
            stop_event.set()

    daemon = Daemon(
        poll=poll,
        review=review,
        recorded=lambda pull: pull.number in (recorded or set()),
        agent_name=AGENT,
        limit=limit,
        heartbeat=tmp_path / "heartbeat",
        poll_interval=POLL_INTERVAL,
        stop=stop_event,
        clock=clock,
    )
    return SimpleNamespace(daemon=daemon, etags=etags, attempts=attempts, reviewed=reviewed, failing=failing)


@pytest.mark.parametrize(
    ("pulls", "recorded", "expected"),
    [
        pytest.param([_pull(3), _pull(1), _pull(2)], set(), [1, 2, 3], id="all-eligible-oldest-first"),
        pytest.param(
            [_pull(1, draft=True), _pull(2, author=AGENT), _pull(3)], set(), [3], id="drafts-and-self-authored-skipped"
        ),
        pytest.param([_pull(1), _pull(2), _pull(3)], {2}, [1, 3], id="recorded-skipped"),
    ],
)
def test_startup_sweep_reviews_eligible_pulls_oldest_first(
    tmp_path: Path, pulls: list[PullSummary], recorded: set[int], expected: list[int]
) -> None:
    run = _daemon(tmp_path, [_listing(*pulls)], recorded=recorded)
    run.daemon.cycle()
    assert [number for number, _ in run.reviewed] == expected


def test_startup_sweep_stops_at_the_pilot_cap_and_continues_next_cycle(tmp_path: Path) -> None:
    run = _daemon(tmp_path, [_listing(_pull(1), _pull(2), _pull(3)), NOT_MODIFIED], limit=2)
    run.daemon.cycle()
    assert run.reviewed == [(1, _sha(1)), (2, _sha(2))]
    run.daemon.cycle()
    assert run.reviewed == [(1, _sha(1)), (2, _sha(2)), (3, _sha(3))]


def test_steady_state_not_modified_does_not_repeat_reviewed_pulls(tmp_path: Path) -> None:
    run = _daemon(tmp_path, [_listing(_pull(1), _pull(2)), NOT_MODIFIED])
    run.daemon.cycle()
    run.daemon.cycle()
    assert run.reviewed == [(1, _sha(1)), (2, _sha(2))]
    assert run.etags == [None, "v1"]


def test_failed_pull_is_retried_only_after_cooldown(tmp_path: Path) -> None:
    now = [0.0]
    run = _daemon(
        tmp_path,
        [_listing(_pull(1)), NOT_MODIFIED, NOT_MODIFIED],
        clock=lambda: now[0],
        failures={1: ReviewError("lint failed")},
    )
    run.daemon.cycle()
    now[0] = RETRY_COOLDOWN_SECONDS - 1
    run.daemon.cycle()
    assert run.attempts == [(1, _sha(1))]
    run.failing.clear()
    now[0] = RETRY_COOLDOWN_SECONDS
    run.daemon.cycle()
    assert run.attempts == [(1, _sha(1)), (1, _sha(1))]
    assert run.reviewed == [(1, _sha(1))]


def test_new_head_of_failed_pull_is_reviewed_without_cooldown(tmp_path: Path) -> None:
    run = _daemon(
        tmp_path,
        [_listing(_pull(1)), _listing(_pull(1, generation=1), etag="v2")],
        failures={1: ReviewError("lint failed")},
    )
    run.daemon.cycle()
    run.failing.clear()
    run.daemon.cycle()
    assert run.reviewed == [(1, _sha(1, 1))]


@pytest.mark.parametrize(
    ("remaining", "reset", "expected_pause", "expected_numbers"),
    [
        pytest.param(RATE_LIMIT_FLOOR - 1, 1000, 600, [], id="below-floor-pauses-until-reset"),
        pytest.param(RATE_LIMIT_FLOOR, 1000, POLL_INTERVAL, [1], id="at-floor-keeps-reviewing"),
    ],
)
def test_rate_limit_floor_pauses_reviews(
    tmp_path: Path, remaining: int, reset: int, expected_pause: float, expected_numbers: list[int]
) -> None:
    run = _daemon(tmp_path, [_listing(_pull(1), remaining=remaining, reset=reset)], clock=lambda: 400.0)
    assert run.daemon.cycle() == expected_pause
    assert [number for number, _ in run.reviewed] == expected_numbers


def test_listing_network_error_is_survived_and_next_cycle_reviews(tmp_path: Path) -> None:
    run = _daemon(tmp_path, [requests.ConnectionError("offline"), _listing(_pull(1))])
    assert run.daemon.cycle() == POLL_INTERVAL
    assert run.reviewed == []
    run.daemon.cycle()
    assert run.reviewed == [(1, _sha(1))]


def test_stop_finishes_current_review_and_skips_the_rest(tmp_path: Path) -> None:
    run = _daemon(tmp_path, [_listing(_pull(1), _pull(2))], stop_after={1})
    run.daemon.run()
    assert run.reviewed == [(1, _sha(1))]


def test_heartbeat_is_touched_on_every_cycle(tmp_path: Path) -> None:
    run = _daemon(tmp_path, [NOT_MODIFIED])
    assert not (tmp_path / "heartbeat").exists()
    run.daemon.cycle()
    assert (tmp_path / "heartbeat").exists()


def test_dry_run_review_is_not_repeated_without_state(tmp_path: Path) -> None:
    run = _daemon(tmp_path, [_listing(_pull(1), etag="v1"), _listing(_pull(1), etag="v2")])
    run.daemon.cycle()
    run.daemon.cycle()
    assert run.reviewed == [(1, _sha(1))]
