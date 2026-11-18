# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Makes this process non-dumpable, so a same-user child (the review agent) cannot read its environment."""

import ctypes
import ctypes.util
import sys

PR_SET_DUMPABLE = 4


class HardeningError(OSError):
    """prctl refused to clear the dumpable flag."""


def make_non_dumpable() -> None:
    """Clear PR_SET_DUMPABLE; without it /proc/<pid>/environ of this process stays readable to same-user children."""
    if not sys.platform.startswith("linux"):
        msg = "non-dumpable hardening needs Linux"
        raise HardeningError(msg)
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        msg = f"prctl(PR_SET_DUMPABLE, 0) failed: errno {errno}"
        raise HardeningError(msg)
