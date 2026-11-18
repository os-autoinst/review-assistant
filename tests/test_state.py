# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the dedup state file."""

from pathlib import Path

import pytest

from review_assistant.state import ReviewRecord, is_reviewed, load_state, save_state, state_key

REPO = "os-autoinst/openQA"
HEAD = "a" * 40
RECORD = ReviewRecord(head_sha=HEAD, review_id=5841967131)


def test_missing_state_file_is_empty(tmp_path: Path) -> None:
    assert load_state(tmp_path / "state.json") == {}


def test_saved_state_round_trips_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    save_state(path, {state_key(REPO, 8079): RECORD})
    assert load_state(path) == {state_key(REPO, 8079): RECORD}
    assert sorted(child.name for child in path.parent.iterdir()) == ["state.json"]


@pytest.mark.parametrize(
    ("state", "number", "head", "expected"),
    [
        pytest.param({state_key(REPO, 8079): RECORD}, 8079, HEAD, True, id="same-head"),
        pytest.param({state_key(REPO, 8079): RECORD}, 8079, "b" * 40, False, id="head-moved"),
        pytest.param({state_key(REPO, 8079): RECORD}, 8080, HEAD, False, id="other-pull"),
        pytest.param({}, 8079, HEAD, False, id="never-reviewed"),
    ],
)
def test_is_reviewed_matches_pull_and_head(
    state: dict[str, ReviewRecord], number: int, head: str, *, expected: bool
) -> None:
    assert is_reviewed(state, REPO, number, head) is expected
