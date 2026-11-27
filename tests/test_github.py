# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the GitHub client, against recorded responses; no network and no real token."""

import json

import pydantic
import pytest
import requests
import responses
from responses import matchers

from review_assistant.github import (
    API,
    DIFF_ACCEPT,
    GitHubError,
    ReviewDraft,
    fetch_pull_request,
    list_open_pulls,
    poll_open_pulls,
    post_review,
    take_read_token,
    take_write_token,
)

REPO = "os-autoinst/openQA"
PULLS = f"{API}/repos/{REPO}/pulls"
READ_TOKEN = "read-token-placeholder"
WRITE_TOKEN = "write-token-placeholder"
HEAD = "a6d255abcbce67a380f6716c94e861869ed64882"
PULL_META = {
    "title": "Fix retry",
    "body": "Retries were unbounded.",
    "head": {"sha": HEAD},
    "additions": 30,
    "deletions": 12,
}
REVIEW = {
    "commit_id": HEAD,
    "event": "COMMENT",
    "body": "One point.",
    "comments": [{"path": "lib/OpenQA/Test.pm", "line": 7, "body": "nit: spacing"}],
}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("  read-token-placeholder \n", id="padded"),
        pytest.param("read-token-placeholder", id="bare"),
    ],
)
def test_read_token_is_stripped_and_taken_out_of_environment(raw: str) -> None:
    environ = {"REVIEW_ASSISTANT_GH_READ_TOKEN": raw, "PATH": "/usr/bin"}
    assert take_read_token(environ) == READ_TOKEN
    assert environ == {"PATH": "/usr/bin"}


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="unset"),
        pytest.param({"REVIEW_ASSISTANT_GH_READ_TOKEN": " \n"}, id="blank"),
    ],
)
def test_missing_read_token_is_rejected_and_environment_stays_clean(environ: dict[str, str]) -> None:
    with pytest.raises(GitHubError, match="REVIEW_ASSISTANT_GH_READ_TOKEN is not set"):
        take_read_token(environ)
    assert "REVIEW_ASSISTANT_GH_READ_TOKEN" not in environ


def test_write_token_is_taken_out_of_environment() -> None:
    environ = {"REVIEW_ASSISTANT_GH_TOKEN": WRITE_TOKEN, "PATH": "/usr/bin"}
    assert take_write_token(environ) == WRITE_TOKEN
    assert environ == {"PATH": "/usr/bin"}


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="unset"),
        pytest.param({"REVIEW_ASSISTANT_GH_TOKEN": "  "}, id="blank"),
    ],
)
def test_missing_write_token_is_empty_and_environment_stays_clean(environ: dict[str, str]) -> None:
    assert not take_write_token(environ)
    assert "REVIEW_ASSISTANT_GH_TOKEN" not in environ


def test_open_pulls_are_listed_across_pages_with_read_token() -> None:
    first = {"Link": f'<{PULLS}?page=2>; rel="next"'}
    with responses.RequestsMock() as rsps:
        rsps.get(
            PULLS,
            json=[
                {
                    "number": 1,
                    "head": {"sha": HEAD},
                    "title": "One",
                    "body": None,
                    "draft": False,
                    "user": {"login": "alice"},
                }
            ],
            headers=first,
            match=[matchers.query_param_matcher({"state": "open", "per_page": "100"})],
        )
        rsps.get(
            PULLS,
            json=[
                {
                    "number": 2,
                    "head": {"sha": "b" * 40},
                    "title": "Two",
                    "body": "text",
                    "draft": True,
                    "user": {"login": "bob"},
                }
            ],
            match=[matchers.query_param_matcher({"page": "2"})],
        )
        pulls = list_open_pulls(READ_TOKEN, REPO)
        auth = rsps.calls[0].request.headers["Authorization"]
    assert [(pull.number, pull.title, pull.body) for pull in pulls] == [(1, "One", ""), (2, "Two", "text")]
    assert pulls[1].head_sha == "b" * 40
    assert [(pull.draft, pull.author) for pull in pulls] == [(False, "alice"), (True, "bob")]
    assert auth == f"Bearer {READ_TOKEN}"


def test_pull_request_is_fetched_with_metadata_and_diff() -> None:
    url = f"{PULLS}/8079"
    with responses.RequestsMock() as rsps:
        rsps.get(url, json=PULL_META, match=[matchers.header_matcher({"Accept": "application/vnd.github+json"})])
        rsps.get(url, body="diff --git a/x b/x\n", match=[matchers.header_matcher({"Accept": DIFF_ACCEPT})])
        pull = fetch_pull_request(READ_TOKEN, REPO, 8079)
    assert (pull.repo, pull.number, pull.head_sha, pull.title, pull.body) == (
        REPO,
        8079,
        HEAD,
        "Fix retry",
        "Retries were unbounded.",
    )
    assert (pull.changed_lines, pull.diff) == (42, "diff --git a/x b/x\n")


def test_listing_http_error_propagates() -> None:
    with responses.RequestsMock() as rsps:
        rsps.get(PULLS, status=403, json={"message": "Forbidden"})
        with pytest.raises(requests.HTTPError):
            list_open_pulls(READ_TOKEN, REPO)


def test_comment_review_is_posted_with_write_token() -> None:
    url = f"{PULLS}/8079/reviews"
    with responses.RequestsMock() as rsps:
        rsps.post(url, json={"id": 5841967131})
        review_id = post_review(WRITE_TOKEN, REPO, 8079, REVIEW)
        request = rsps.calls[0].request
    assert review_id == 5841967131
    assert request.headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
    assert isinstance(request.body, bytes)
    assert json.loads(request.body) == REVIEW


def test_approve_review_is_posted_with_write_token() -> None:
    url = f"{PULLS}/8079/reviews"
    with responses.RequestsMock() as rsps:
        rsps.post(url, json={"id": 5841967132})
        review_id = post_review(WRITE_TOKEN, REPO, 8079, {**REVIEW, "event": "APPROVE"})
        request = rsps.calls[0].request
    assert review_id == 5841967132
    assert isinstance(request.body, bytes)
    assert json.loads(request.body)["event"] == "APPROVE"


@pytest.mark.parametrize(
    "event",
    [
        pytest.param("REQUEST_CHANGES", id="request-changes"),
        pytest.param("DISMISS", id="dismiss"),
        pytest.param(None, id="missing"),
    ],
)
def test_review_event_outside_comment_and_approve_is_rejected_before_any_request(event: str | None) -> None:
    review = {**REVIEW, "event": event} if event else {k: v for k, v in REVIEW.items() if k != "event"}
    with responses.RequestsMock() as rsps:
        with pytest.raises(pydantic.ValidationError):
            post_review(WRITE_TOKEN, REPO, 8079, review)
        assert len(rsps.calls) == 0


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("LGTM", id="filler-lgtm"),
        pytest.param("Looks good to me.", id="filler-looks-good"),
        pytest.param("+1", id="filler-plus-one"),
    ],
)
def test_review_draft_rejects_empty_and_filler_body(body: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        ReviewDraft.model_validate({**REVIEW, "body": body})


def test_poll_lists_most_recently_updated_first_and_returns_validators() -> None:
    first = {
        "Link": f'<{PULLS}?page=2>; rel="next"',
        "ETag": 'W/"etag-1"',
        "X-RateLimit-Remaining": "4999",
        "X-RateLimit-Reset": "1700000000",
    }
    with responses.RequestsMock() as rsps:
        rsps.get(
            PULLS,
            json=[
                {
                    "number": 7,
                    "head": {"sha": HEAD},
                    "title": "Seven",
                    "body": None,
                    "draft": True,
                    "user": {"login": "carol"},
                }
            ],
            headers=first,
            match=[
                matchers.query_param_matcher({
                    "state": "open",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": "100",
                })
            ],
        )
        rsps.get(
            PULLS,
            json=[
                {
                    "number": 8,
                    "head": {"sha": "c" * 40},
                    "title": "Eight",
                    "body": "",
                    "draft": False,
                    "user": {"login": "dave"},
                }
            ],
            match=[matchers.query_param_matcher({"page": "2"})],
        )
        result = poll_open_pulls(READ_TOKEN, REPO, None)
        sent = rsps.calls[0].request.headers
    assert [(pull.number, pull.draft, pull.author) for pull in result.pulls or []] == [
        (7, True, "carol"),
        (8, False, "dave"),
    ]
    assert (result.etag, result.remaining, result.reset) == ('W/"etag-1"', 4999, 1700000000)
    assert "If-None-Match" not in sent


def test_poll_sends_etag_and_reports_not_modified_without_pulls() -> None:
    with responses.RequestsMock() as rsps:
        rsps.get(
            PULLS,
            status=304,
            headers={"ETag": 'W/"etag-1"', "X-RateLimit-Remaining": "4990"},
            match=[matchers.header_matcher({"If-None-Match": 'W/"etag-1"'})],
        )
        result = poll_open_pulls(READ_TOKEN, REPO, 'W/"etag-1"')
    assert result.pulls is None
    assert (result.etag, result.remaining, result.reset) == ('W/"etag-1"', 4990, None)


def test_poll_http_error_propagates() -> None:
    with responses.RequestsMock() as rsps:
        rsps.get(PULLS, status=500)
        with pytest.raises(requests.HTTPError):
            poll_open_pulls(READ_TOKEN, REPO, None)
