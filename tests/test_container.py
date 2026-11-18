# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the podman argv of one review pass: hardening, mount modes and absence of credential values."""

import dataclasses
import subprocess  # noqa: S404 - builds CompletedProcess fixtures only
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from pytest_mock import MockerFixture

from review_assistant.config import load_config
from review_assistant.container import IMAGE, HostPaths, build_run_argv, default_host_paths, main

REPO = Path(__file__).resolve().parents[1]
READ_TOKEN = "read-token-placeholder"


def _paths(tmp_path: Path) -> HostPaths:
    return HostPaths(
        adc_file=tmp_path / "adc.json",
        read_token_file=tmp_path / "read-token",
        env_file=tmp_path / "write.env",
        config_file=tmp_path / "reviewer.yaml",
        hermes_config_file=tmp_path / "config.yaml",
        state_dir=tmp_path / "state",
    )


def _mounts(argv: list[str]) -> dict[str, str]:
    values = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-v"]
    return {value.split(":")[1]: value.split(":")[2] for value in values}


@pytest.mark.parametrize(
    "flag",
    [
        pytest.param("--rm", id="removed-after-pass"),
        pytest.param("--userns=keep-id", id="host-user-owns-mounts"),
        pytest.param("--read-only", id="read-only-root"),
        pytest.param("--tmpfs=/tmp", id="writable-tmp"),
        pytest.param("--tmpfs=/hermes:mode=1777", id="writable-hermes-home"),
        pytest.param("--cap-drop=ALL", id="no-capabilities"),
        pytest.param("--security-opt=no-new-privileges", id="no-privilege-escalation"),
    ],
)
def test_run_is_hardened(tmp_path: Path, flag: str) -> None:
    assert flag in build_run_argv(_paths(tmp_path), [])


@pytest.mark.parametrize(
    ("container_path", "mode"),
    [
        pytest.param("/run/secrets/gcloud/application_default_credentials.json", "ro", id="adc-read-only"),
        pytest.param("/config/reviewer.yaml", "ro", id="config-read-only"),
        pytest.param("/hermes/config.yaml", "ro", id="hermes-config-read-only"),
        pytest.param("/state", "rw", id="state-directory-writable-for-atomic-replace"),
    ],
)
def test_mount_modes(tmp_path: Path, container_path: str, mode: str) -> None:
    assert _mounts(build_run_argv(_paths(tmp_path), []))[container_path] == mode


def test_adc_is_found_through_gcloud_config_not_as_service_account_key(tmp_path: Path) -> None:
    argv = build_run_argv(_paths(tmp_path), [])
    envs = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-e"]
    assert "CLOUDSDK_CONFIG=/run/secrets/gcloud" in envs
    assert not any(env.startswith("GOOGLE_APPLICATION_CREDENTIALS=") for env in envs)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/run/secrets/gh-read-token", id="read-token-not-mounted"),
        pytest.param("/opt/skills", id="baked-skill-not-shadowed"),
    ],
)
def test_path_is_not_mounted(tmp_path: Path, path: str) -> None:
    assert path not in _mounts(build_run_argv(_paths(tmp_path), []))


def test_read_token_is_passed_by_name_only(tmp_path: Path) -> None:
    argv = build_run_argv(_paths(tmp_path), [])
    assert argv[argv.index("REVIEW_ASSISTANT_GH_READ_TOKEN") - 1] == "-e"
    assert not any("REVIEW_ASSISTANT_GH_READ_TOKEN=" in arg for arg in argv)


def test_write_token_is_passed_only_as_env_file_path(tmp_path: Path) -> None:
    argv = build_run_argv(_paths(tmp_path), ["--post"])
    assert argv[argv.index("--env-file") + 1] == str(tmp_path / "write.env")
    assert not any("REVIEW_ASSISTANT_GH_TOKEN" in arg for arg in argv)
    assert not any("github_pat_" in arg or "ghp_" in arg for arg in argv)


def test_review_arguments_follow_image(tmp_path: Path) -> None:
    argv = build_run_argv(_paths(tmp_path), ["--post", "--limit", "3"])
    assert argv[argv.index(IMAGE) :] == [
        IMAGE,
        "run",
        "--config",
        "/config/reviewer.yaml",
        "--state-file",
        "/state/state.json",
        "--skill-cache",
        "/opt/skills",
        "--post",
        "--limit",
        "3",
    ]


@pytest.mark.parametrize(
    "field",
    [
        pytest.param("adc_file", id="adc"),
        pytest.param("read_token_file", id="read-token"),
        pytest.param("env_file", id="write-env"),
    ],
)
def test_secrets_are_outside_the_repository(field: str) -> None:
    assert not getattr(default_host_paths(REPO), field).is_relative_to(REPO)


def test_image_bakes_the_skill_at_the_pin_of_the_example_config() -> None:
    containerfile = (REPO / "Containerfile").read_text(encoding="utf-8")
    pin = load_config(REPO / "config" / "reviewer.example.yaml").skill.sha
    assert f"ARG SKILL_SHA={pin}\n" in containerfile
    configmap = yaml.safe_load((REPO / "deploy/kubernetes/base/configmap.yaml").read_text(encoding="utf-8"))
    assert yaml.safe_load(configmap["data"]["reviewer.yaml"])["skill"]["sha"] == pin


@pytest.fixture
def podman(mocker: MockerFixture, tmp_path: Path) -> MagicMock:
    token_file = tmp_path / "read-token"
    token_file.write_text(f"{READ_TOKEN}\n", encoding="utf-8")
    paths = dataclasses.replace(_paths(tmp_path), read_token_file=token_file)
    mocker.patch("review_assistant.container.default_host_paths", return_value=paths)
    return mocker.patch("review_assistant.container.subprocess.run", return_value=subprocess.CompletedProcess([], 4))


@pytest.mark.usefixtures("podman")
def test_main_creates_the_state_dir_before_podman_runs(tmp_path: Path) -> None:
    assert not (tmp_path / "state").exists()
    main([])
    assert (tmp_path / "state").is_dir()


def test_main_returns_podman_exit_code_and_passes_review_flags(podman: MagicMock) -> None:
    assert main(["--post", "--limit", "2"]) == 4
    argv = podman.call_args.args[0]
    assert argv[0:2] == ["podman", "run"]
    assert argv[-3:] == ["--post", "--limit", "2"]


def test_main_without_post_runs_a_dry_run(podman: MagicMock) -> None:
    main([])
    assert "--post" not in podman.call_args.args[0]


def test_main_check_isolation_runs_the_check_command_without_review_flags(podman: MagicMock) -> None:
    main(["--check-isolation", "--post"])
    argv = podman.call_args.args[0]
    assert argv[argv.index(IMAGE) + 1] == "check-isolation"
    assert "--post" not in argv


def test_main_hands_read_token_to_podman_through_its_environment_only(podman: MagicMock) -> None:
    main([])
    assert podman.call_args.kwargs["env"]["REVIEW_ASSISTANT_GH_READ_TOKEN"] == READ_TOKEN
    assert not any(READ_TOKEN in arg for arg in podman.call_args.args[0])
