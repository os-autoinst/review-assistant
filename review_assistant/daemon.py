# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Continuous review loop: poll open pull requests, review them one at a time, survive transient failures."""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pydantic
import requests

from review_assistant.github import PollResult, PullSummary
from review_assistant.review import ReviewError

RATE_LIMIT_FLOOR = 200
RETRY_COOLDOWN_SECONDS = 900
PER_PULL_ERRORS = (ReviewError, requests.RequestException, json.JSONDecodeError, pydantic.ValidationError)

Poll = Callable[[str | None], PollResult]
Review = Callable[[PullSummary], None]
Recorded = Callable[[PullSummary], bool]
Key = tuple[int, str]

log = logging.getLogger(__name__)


@dataclass
class Daemon:
    """Reviews pull requests in FIFO order, one at a time; the first cycle is the startup sweep."""

    poll: Poll
    review: Review
    recorded: Recorded
    agent_name: str
    limit: int
    heartbeat: Path
    poll_interval: float
    stop: threading.Event
    clock: Callable[[], float] = time.time
    _etag: str | None = field(default=None, init=False)
    _known: dict[int, PullSummary] = field(default_factory=dict, init=False)
    _done: set[Key] = field(default_factory=set, init=False)
    _failed_at: dict[Key, float] = field(default_factory=dict, init=False)

    def run(self) -> None:
        """Run cycles until stop is set; a review in progress completes before the loop returns."""
        while not self.stop.is_set():
            self.stop.wait(self.cycle())

    def cycle(self) -> float:
        """Run one poll and review pass; return the seconds to pause before the next one."""
        self._beat()
        try:
            result = self.poll(self._etag)
        except requests.RequestException as error:
            log.warning("listing open pull requests failed, retrying: %s", error)
            return self.poll_interval
        self._absorb(result)
        if self._rate_limited(result):
            return self._rate_limit_pause(result)
        self._review_due()
        return self.poll_interval

    def _absorb(self, result: PollResult) -> None:
        if result.pulls is None:
            return
        self._etag = result.etag
        self._known = {pull.number: pull for pull in result.pulls}
        heads = {(pull.number, pull.head_sha) for pull in result.pulls}
        self._failed_at = {key: at for key, at in self._failed_at.items() if key in heads}

    @staticmethod
    def _rate_limited(result: PollResult) -> bool:
        return result.remaining is not None and result.remaining < RATE_LIMIT_FLOOR

    def _rate_limit_pause(self, result: PollResult) -> float:
        log.warning("GitHub rate limit low at %s remaining; pausing reviews until reset", result.remaining)
        return max(self.poll_interval, (result.reset or 0) - self.clock())

    def _review_due(self) -> None:
        now = self.clock()
        reviewed = 0
        for pull in sorted(self._known.values(), key=lambda pull: pull.number):
            if self.stop.is_set() or reviewed >= self.limit:
                return
            if self._eligible(pull, now):
                self._review(pull)
                reviewed += 1

    def _eligible(self, pull: PullSummary, now: float) -> bool:
        key = (pull.number, pull.head_sha)
        if pull.draft or pull.author == self.agent_name:
            return False
        if self.recorded(pull) or key in self._done:
            return False
        failed_at = self._failed_at.get(key)
        return failed_at is None or now - failed_at >= RETRY_COOLDOWN_SECONDS

    def _review(self, pull: PullSummary) -> None:
        self._beat()
        try:
            self.review(pull)
        except PER_PULL_ERRORS as error:
            log.warning("#%s at %s not recorded, retry after cooldown: %s", pull.number, pull.head_sha, error)
            self._failed_at[pull.number, pull.head_sha] = self.clock()
            return
        self._done.add((pull.number, pull.head_sha))

    def _beat(self) -> None:
        self.heartbeat.touch()
