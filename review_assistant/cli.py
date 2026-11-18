# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Command line entry point: review open pull requests with Hermes, post only with --post."""

import argparse
import json
import logging
import os
import signal
import subprocess  # noqa: S404 - runs the fixed isolation check script, argv only, no shell
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from review_assistant.config import ConfigError, ReviewerConfig, load_config
from review_assistant.daemon import PER_PULL_ERRORS, Daemon
from review_assistant.github import (
    GH_WRITE_ENV,
    GitHubError,
    PullSummary,
    ReviewDraft,
    fetch_pull_request,
    list_open_pulls,
    poll_open_pulls,
    post_review,
    take_read_token,
    take_write_token,
)
from review_assistant.hardening import HardeningError, make_non_dumpable
from review_assistant.review import ReviewError, Workspace, agent_env, ensure_clone, review_pull_request
from review_assistant.skill import SkillCheckoutError, checkout_skill
from review_assistant.state import ReviewRecord, State, is_reviewed, load_state, save_state, state_key

REPO = "os-autoinst/openQA"
REPO_URL = f"https://github.com/{REPO}.git"
SKILL_SUBDIR = Path("skills") / "openqa"
DEFAULT_CONFIG = Path("config/reviewer.yaml")
DEFAULT_STATE = Path.home() / ".local/state/review-assistant/state.json"
DEFAULT_SKILL_CACHE = Path.home() / ".cache/review-assistant/skills"
ISOLATION_CHECK = Path("/opt/check-isolation.sh")
DEFAULT_HEARTBEAT = Path("/tmp/heartbeat")  # noqa: S108 - liveness file of the container, tmpfs in the pod
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

log = logging.getLogger(__name__)


@dataclass
class _Session:
    config: ReviewerConfig
    read_token: str
    post_token: str | None
    skill_root: Path
    skill_checkout: Path
    clone: Path
    scratch_root: Path
    state_file: Path
    state: State


def _add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="reviewer configuration file")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE, help="local record of posted reviews")
    parser.add_argument("--skill-cache", type=Path, default=DEFAULT_SKILL_CACHE, help="directory for the pinned skill")
    parser.add_argument("--clone", type=Path, help="openQA clone to reuse; cloned into the scratch root if absent")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review-assistant")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="review open pull requests of os-autoinst/openQA")
    _add_shared_arguments(run)
    run.add_argument("--post", action="store_true", help="post the reviews; dry-run prints them instead")
    run.add_argument("--limit", type=int, help="new reviews per run (default: pilot_cap of the config)")
    daemon = commands.add_parser("daemon", help="review open pull requests of os-autoinst/openQA continuously")
    _add_shared_arguments(daemon)
    daemon.add_argument("--post", action="store_true", help="post the reviews; dry-run prints them instead")
    daemon.add_argument("--poll-interval", type=float, default=60, help="seconds between listings of open pulls")
    daemon.add_argument("--heartbeat-file", type=Path, default=DEFAULT_HEARTBEAT, help="liveness timestamp file")
    check = commands.add_parser("check-isolation", help="check what the review agent can reach, inside the image")
    _add_shared_arguments(check)
    check.set_defaults(post=True)
    return parser


def _review_one(session: _Session, summary: PullSummary) -> None:
    pull = fetch_pull_request(session.read_token, REPO, summary.number)
    secrets = (session.read_token, session.post_token or "")
    workspace = Workspace(session.skill_root, session.scratch_root, session.clone, secrets)
    review_path = review_pull_request(session.config, pull, workspace)
    text = review_path.read_text(encoding="utf-8")
    review = json.loads(text)
    ReviewDraft.model_validate(review)
    if review.get("commit_id") != pull.head_sha:
        msg = f"review commit_id {review.get('commit_id')} is not the checked head {pull.head_sha}"
        raise ReviewError(msg)
    if session.post_token is None:
        sys.stdout.write(f"# {REPO}#{pull.number} at {pull.head_sha}\n{text}\n")
        return
    review_id = post_review(session.post_token, REPO, pull.number, review)
    session.state[state_key(REPO, pull.number)] = ReviewRecord(head_sha=pull.head_sha, review_id=review_id)
    save_state(session.state_file, session.state)
    log.info("posted review %s on %s#%s at %s", review_id, REPO, pull.number, pull.head_sha)


def _review_pulls(session: _Session, pulls: Sequence[PullSummary], limit: int) -> int:
    todo = [
        pull
        for pull in pulls
        if not pull.draft
        and pull.author != session.config.agent_name
        and not is_reviewed(session.state, REPO, pull.number, pull.head_sha)
    ][:limit]
    failures = 0
    for summary in todo:
        try:
            _review_one(session, summary)
        except PER_PULL_ERRORS as error:
            log.warning("%s#%s not recorded: %s", REPO, summary.number, error)
            failures += 1
    return EXIT_FAILED if failures else EXIT_OK


def _post_token(*, post: bool) -> str | None:
    token = take_write_token(os.environ)
    if post and not token:
        msg = f"{GH_WRITE_ENV} is not set; --post needs it"
        raise GitHubError(msg)
    return token if post else None


@contextmanager
def _session(args: argparse.Namespace, config: ReviewerConfig) -> Iterator[_Session]:
    post_token = _post_token(post=args.post)
    read_token = take_read_token(os.environ)
    skill_checkout = checkout_skill(config.skill, args.skill_cache)
    with tempfile.TemporaryDirectory(prefix="review-assistant-") as scratch_root:
        clone = args.clone if args.clone is not None else Path(scratch_root) / "openQA"
        ensure_clone(REPO_URL, clone)
        yield _Session(
            config=config,
            read_token=read_token,
            post_token=post_token,
            skill_root=skill_checkout / SKILL_SUBDIR,
            skill_checkout=skill_checkout,
            clone=clone,
            scratch_root=Path(scratch_root),
            state_file=args.state_file,
            state=load_state(args.state_file),
        )


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    limit = args.limit if args.limit is not None else config.pilot_cap
    with _session(args, config) as session:
        pulls = list_open_pulls(session.read_token, REPO)
        return _review_pulls(session, pulls, limit)


def _request_stop(stop: threading.Event) -> Callable[[int, FrameType | None], None]:
    def handler(_signum: int, _frame: FrameType | None) -> None:
        log.info("stop requested; finishing the current review")
        stop.set()

    return handler


def _install_stop_handlers(stop: threading.Event) -> None:
    handler = _request_stop(stop)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, handler)


def _daemon(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    stop = threading.Event()
    _install_stop_handlers(stop)
    with _session(args, config) as session:
        Daemon(
            poll=lambda etag: poll_open_pulls(session.read_token, REPO, etag),
            review=lambda summary: _review_one(session, summary),
            recorded=lambda summary: is_reviewed(session.state, REPO, summary.number, summary.head_sha),
            agent_name=config.agent_name,
            limit=config.pilot_cap,
            heartbeat=args.heartbeat_file,
            poll_interval=args.poll_interval,
            stop=stop,
        ).run()
    return EXIT_OK


def _check_isolation(args: argparse.Namespace) -> int:
    if not ISOLATION_CHECK.is_file():
        sys.stderr.write(f"error: {ISOLATION_CHECK} not found; check-isolation runs inside the image\n")
        return EXIT_USAGE
    with _session(args, load_config(args.config)) as session:
        scratch = session.scratch_root / "check"
        scratch.mkdir()
        paths = (session.state_file, session.clone, session.skill_checkout, scratch)
        argv = [*agent_env(scratch), str(ISOLATION_CHECK), *map(str, paths)]
        return subprocess.run(argv, check=False).returncode  # noqa: S603 - fixed script path, no shell


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": _run,
    "daemon": _daemon,
    "check-isolation": _check_isolation,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the review CLI and return its exit code."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        make_non_dumpable()
        return _COMMANDS[args.command](args)
    except (GitHubError, HardeningError, ConfigError, SkillCheckoutError, ReviewError) as error:
        sys.stderr.write(f"error: {error}\n")
        return EXIT_USAGE
