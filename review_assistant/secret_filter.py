# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Redacts credentials from agent-authored text: the review and its errors are the agent's only output that leaves."""

import json
import re
from collections.abc import Iterable

REDACTED = "[redacted]"
_CREDENTIAL = re.compile(
    r"github_pat_\w{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|ya29\.[\w-]{20,}|1//0[\w-]{20,}|GOCSPX-[\w-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def redact(text: str, known: Iterable[str]) -> str:
    """Return text with the known values and any GitHub or Google credential pattern replaced."""
    for value in filter(None, known):
        text = text.replace(value, REDACTED)
    return _CREDENTIAL.sub(REDACTED, text)


def _decoded(text: str) -> str:
    try:
        return json.dumps(json.loads(text), ensure_ascii=False)
    except json.JSONDecodeError:
        return ""


def holds_secret(text: str, known: Iterable[str]) -> bool:
    """Report whether text, or the JSON it decodes to (where escapes would hide a value), holds a credential."""
    values = tuple(known)
    return any(redact(form, values) != form for form in (text, _decoded(text)))
