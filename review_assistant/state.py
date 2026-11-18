# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Local record of posted reviews, keyed by repository and pull request number."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter


class ReviewRecord(BaseModel):
    """A review posted at a given head commit."""

    model_config = ConfigDict(frozen=True)

    head_sha: str
    review_id: int


State = dict[str, ReviewRecord]
_STATE_ADAPTER: TypeAdapter[State] = TypeAdapter(State)


def state_key(repo: str, number: int) -> str:
    """Return the state key of a pull request."""
    return f"{repo}#{number}"


def load_state(path: Path) -> State:
    """Load the state file at path; a missing file is an empty state."""
    if not path.exists():
        return {}
    return _STATE_ADAPTER.validate_json(path.read_bytes())


def save_state(path: Path, state: State) -> None:
    """Write state to path atomically, so an interrupted run never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(_STATE_ADAPTER.dump_json(state, indent=2))
    tmp.replace(path)


def is_reviewed(state: State, repo: str, number: int, head_sha: str) -> bool:
    """Report whether a review was already posted for this pull request at head_sha."""
    record = state.get(state_key(repo, number))
    return record is not None and record.head_sha == head_sha
