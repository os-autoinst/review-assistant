# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""GitHub access with two credentials: a read-only token for listing and diffs, the write token for reviews."""

from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator

from review_assistant.review import PullRequest, ReviewError, validate_review_summary

API = "https://api.github.com"
JSON_ACCEPT = "application/vnd.github+json"
DIFF_ACCEPT = "application/vnd.github.diff"
TIMEOUT_SECONDS = 60
GH_READ_ENV = "REVIEW_ASSISTANT_GH_READ_TOKEN"
GH_WRITE_ENV = "REVIEW_ASSISTANT_GH_TOKEN"


class GitHubError(RuntimeError):
    """A credential is missing or empty."""


class PullSummary(BaseModel):
    """Open pull request as listed; enough to decide whether it needs a review."""

    model_config = ConfigDict(frozen=True)

    number: int
    head_sha: str
    title: str
    body: str
    draft: bool
    author: str


@dataclass(frozen=True)
class PollResult:
    """One conditional listing: pulls is None when GitHub answered 304 Not Modified."""

    pulls: list[PullSummary] | None
    etag: str | None
    remaining: int | None
    reset: int | None


class InlineComment(BaseModel):
    """Comment anchored to one line of the diff."""

    model_config = ConfigDict(frozen=True)

    path: str
    line: int
    body: str


class ReviewDraft(BaseModel):
    """Review JSON for POST /pulls/N/reviews; only COMMENT and APPROVE are accepted."""

    model_config = ConfigDict(frozen=True)

    commit_id: str
    event: Literal["COMMENT", "APPROVE"]
    body: str
    comments: list[InlineComment] = Field(default_factory=list)

    @field_validator("body")
    @staticmethod
    def _validate_body(value: str) -> str:
        try:
            validate_review_summary(value)
        except ReviewError as error:
            raise ValueError(str(error)) from None
        return value


def take_read_token(environ: MutableMapping[str, str]) -> str:
    """Return the read-only token and remove it from environ, so no child process inherits it."""
    token = environ.pop(GH_READ_ENV, "").strip()
    if not token:
        msg = f"{GH_READ_ENV} is not set"
        raise GitHubError(msg)
    return token


def take_write_token(environ: MutableMapping[str, str]) -> str:
    """Return the write token and remove it from environ, so no child process inherits it."""
    return environ.pop(GH_WRITE_ENV, "").strip()


def _session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    return session


def _get(
    session: requests.Session, url: str, accept: str, params: Mapping[str, str] | None = None
) -> requests.Response:
    response = session.get(url, headers={"Accept": accept}, params=params, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response


def _follow(session: requests.Session, response: requests.Response) -> Iterator[dict[str, Any]]:
    current: requests.Response | None = response
    while current is not None:
        yield from current.json()
        next_url = current.links.get("next", {}).get("url")
        current = _get(session, next_url, JSON_ACCEPT) if next_url else None


def _paginate(session: requests.Session, url: str, params: Mapping[str, str]) -> Iterator[dict[str, Any]]:
    yield from _follow(session, _get(session, url, JSON_ACCEPT, params))


def _summary(item: Mapping[str, Any]) -> PullSummary:
    return PullSummary(
        number=item["number"],
        head_sha=item["head"]["sha"],
        title=item["title"],
        body=item["body"] or "",
        draft=item["draft"],
        author=item["user"]["login"],
    )


def _int_header(response: requests.Response, name: str) -> int | None:
    value = response.headers.get(name)
    return int(value) if value is not None else None


def list_open_pulls(token: str, repo: str) -> list[PullSummary]:
    """List open pull requests of repo, following pagination."""
    session = _session(token)
    url = f"{API}/repos/{repo}/pulls"
    return [_summary(item) for item in _paginate(session, url, {"state": "open", "per_page": "100"})]


def poll_open_pulls(token: str, repo: str, etag: str | None) -> PollResult:
    """List open pulls of repo, most recently updated first, unless etag still matches (304, no pulls)."""
    session = _session(token)
    headers = {"Accept": JSON_ACCEPT} | ({"If-None-Match": etag} if etag else {})
    response = session.get(
        f"{API}/repos/{repo}/pulls",
        headers=headers,
        params={"state": "open", "sort": "updated", "direction": "desc", "per_page": "100"},
        timeout=TIMEOUT_SECONDS,
    )
    remaining = _int_header(response, "X-RateLimit-Remaining")
    reset = _int_header(response, "X-RateLimit-Reset")
    if response.status_code == requests.codes.not_modified:
        return PollResult(pulls=None, etag=etag, remaining=remaining, reset=reset)
    response.raise_for_status()
    pulls = [_summary(item) for item in _follow(session, response)]
    return PollResult(pulls=pulls, etag=response.headers.get("ETag"), remaining=remaining, reset=reset)


def fetch_pull_request(token: str, repo: str, number: int) -> PullRequest:
    """Fetch the metadata and diff of one pull request."""
    session = _session(token)
    url = f"{API}/repos/{repo}/pulls/{number}"
    meta = _get(session, url, JSON_ACCEPT).json()
    diff = _get(session, url, DIFF_ACCEPT).text
    return PullRequest(
        repo=repo,
        number=number,
        head_sha=meta["head"]["sha"],
        title=meta["title"],
        body=meta["body"] or "",
        diff=diff,
        changed_lines=meta["additions"] + meta["deletions"],
    )


def post_review(token: str, repo: str, number: int, review: Mapping[str, Any]) -> int:
    """Post a COMMENT or APPROVE review and return its id; any other event fails validation before a request."""
    draft = ReviewDraft.model_validate(review)
    response = _session(token).post(
        f"{API}/repos/{repo}/pulls/{number}/reviews",
        json=draft.model_dump(),
        headers={"Accept": JSON_ACCEPT},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return int(response.json()["id"])
