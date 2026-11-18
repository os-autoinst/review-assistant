# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the secret filter applied to agent-authored text before it is printed, logged or posted."""

import json
from collections.abc import Callable

import pytest

from review_assistant.secret_filter import holds_secret, redact

KNOWN = "read-token-placeholder"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"body": "Looks fine."}', id="plain-review"),
        pytest.param('{"body": "See ghp_short and ya29. in the docs"}', id="prefixes-without-a-value"),
        pytest.param('{"body": "uses github_pat_ in a comment"}', id="pat-prefix-alone"),
        pytest.param("not json at all", id="unparsable-text"),
    ],
)
def test_clean_text_holds_no_secret(text: str) -> None:
    assert not holds_secret(text, [KNOWN, ""])


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(KNOWN, id="known-value"),
        pytest.param("ghp_" + "a" * 36, id="github-classic"),
        pytest.param("github_pat_" + "A1_" * 10, id="github-fine-grained"),
        pytest.param("ghs_" + "b" * 36, id="github-app"),
        pytest.param("ya29." + "c" * 40, id="google-access-token"),
        pytest.param("1//0" + "d" * 40, id="google-refresh-token"),
        pytest.param("GOCSPX-" + "e" * 20, id="google-client-secret"),
        pytest.param("-----BEGIN RSA PRIVATE KEY-----", id="private-key"),
    ],
)
@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(lambda value: json.dumps({"body": value}), id="in-body"),
        pytest.param(lambda value: json.dumps({"comments": [{"path": "x", "line": 1, "body": value}]}), id="inline"),
        pytest.param(
            lambda value: json.dumps({"body": value}, ensure_ascii=True).replace("e", "\\u0065"), id="escaped"
        ),
    ],
)
def test_credential_is_found_wherever_the_review_carries_it(value: str, wrap: Callable[[str], str]) -> None:
    assert holds_secret(wrap(value), [KNOWN])


def test_redact_replaces_known_values_and_patterns_only() -> None:
    text = f"lint: {KNOWN} and ghp_{'a' * 36} in line 3"
    assert redact(text, [KNOWN, ""]) == "lint: [redacted] and [redacted] in line 3"
