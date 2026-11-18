# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Host-side builder of the rootless podman run for one review pass; it holds paths, never credential values."""

import argparse
import os
import subprocess  # noqa: S404 - podman is an external interface, argv only, no shell
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from review_assistant.github import GH_READ_ENV

IMAGE = "review-assistant:dev"
CONTAINER_CONFIG = "/config/reviewer.yaml"
CONTAINER_STATE_DIR = "/state"
CONTAINER_STATE = f"{CONTAINER_STATE_DIR}/state.json"
CONTAINER_SKILL_CACHE = "/opt/skills"
CONTAINER_GCLOUD_DIR = "/run/secrets/gcloud"
CONTAINER_ADC = f"{CONTAINER_GCLOUD_DIR}/application_default_credentials.json"
CONTAINER_HERMES_HOME = "/hermes"
CONTAINER_HERMES_CONFIG = f"{CONTAINER_HERMES_HOME}/config.yaml"


@dataclass(frozen=True)
class HostPaths:
    """Host paths for one container run; the secrets among them stay outside the repository.

    The read token file is read on the host and passed as an environment variable, never mounted, so the
    agent finds no copy of it once the CLI has taken it out of its environment.
    """

    adc_file: Path
    read_token_file: Path
    env_file: Path
    config_file: Path
    hermes_config_file: Path
    state_dir: Path
    image: str = IMAGE


def build_run_argv(paths: HostPaths, review_args: Sequence[str], command: str = "run") -> list[str]:
    """Return the podman argv that runs one review pass (or another CLI command) in a fresh container."""
    return [
        "podman",
        "run",
        "--rm",
        "--userns=keep-id",
        "--read-only",
        "--tmpfs=/tmp",
        f"--tmpfs={CONTAINER_HERMES_HOME}:mode=1777",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--env-file",
        str(paths.env_file),
        "-e",
        GH_READ_ENV,
        "-e",
        "HOME=/tmp",
        "-e",
        f"HERMES_HOME={CONTAINER_HERMES_HOME}",
        "-e",
        f"CLOUDSDK_CONFIG={CONTAINER_GCLOUD_DIR}",
        "-v",
        f"{paths.adc_file}:{CONTAINER_ADC}:ro",
        "-v",
        f"{paths.config_file}:{CONTAINER_CONFIG}:ro",
        "-v",
        f"{paths.hermes_config_file}:{CONTAINER_HERMES_CONFIG}:ro",
        "-v",
        f"{paths.state_dir}:{CONTAINER_STATE_DIR}:rw",
        paths.image,
        command,
        "--config",
        CONTAINER_CONFIG,
        "--state-file",
        CONTAINER_STATE,
        "--skill-cache",
        CONTAINER_SKILL_CACHE,
        *review_args,
    ]


def default_host_paths(repo: Path) -> HostPaths:
    """Return the host paths of this installation: secrets under ~/.config, state under ~/.local/state."""
    home = Path.home()
    config_home = home / ".config" / "review-assistant"
    return HostPaths(
        adc_file=home / ".config" / "gcloud" / "application_default_credentials.json",
        read_token_file=config_home / "read-token",
        env_file=config_home / "write.env",
        config_file=repo / "config" / "reviewer.yaml",
        hermes_config_file=repo / "deploy" / "hermes" / "config.yaml",
        state_dir=home / ".local" / "state" / "review-assistant",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one review pass in a container and return podman's exit code."""
    parser = argparse.ArgumentParser(prog="review-assistant-container")
    parser.add_argument("--post", action="store_true", help="post the reviews instead of a dry run")
    parser.add_argument("--limit", type=int, help="new reviews per run")
    parser.add_argument("--check-isolation", action="store_true", help="check what the agent can reach, review nothing")
    args = parser.parse_args(argv)
    review_args = (["--post"] if args.post else []) + (["--limit", str(args.limit)] if args.limit is not None else [])
    command, review_args = ("check-isolation", []) if args.check_isolation else ("run", review_args)
    paths = default_host_paths(Path(__file__).resolve().parents[1])
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ | {GH_READ_ENV: paths.read_token_file.read_text(encoding="utf-8").strip()}
    return subprocess.run(build_run_argv(paths, review_args, command), check=False, env=env).returncode  # noqa: S603 - argv built here, no shell
