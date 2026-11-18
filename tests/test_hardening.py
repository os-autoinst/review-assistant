# Copyright SUSE LLC
# SPDX-License-Identifier: Apache-2.0
"""Tests for the non-dumpable guard, with prctl replaced by a fake libc."""

import ctypes
import sys
from types import SimpleNamespace

import pytest
from pytest_mock import MockerFixture

from review_assistant import hardening
from review_assistant.hardening import PR_SET_DUMPABLE, HardeningError, make_non_dumpable


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_prctl_clears_dumpable_flag(mocker: MockerFixture) -> None:
    libc = SimpleNamespace(prctl=mocker.Mock(return_value=0))
    mocker.patch.object(hardening.ctypes, "CDLL", return_value=libc)
    make_non_dumpable()
    libc.prctl.assert_called_once_with(PR_SET_DUMPABLE, 0, 0, 0, 0)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_prctl_failure_stops_the_run(mocker: MockerFixture) -> None:
    libc = SimpleNamespace(prctl=mocker.Mock(return_value=-1))
    mocker.patch.object(hardening.ctypes, "CDLL", return_value=libc)
    mocker.patch.object(hardening.ctypes, "get_errno", return_value=1)
    with pytest.raises(HardeningError, match="errno 1"):
        make_non_dumpable()


def test_non_linux_is_refused(mocker: MockerFixture) -> None:
    mocker.patch.object(hardening.sys, "platform", "darwin")
    with pytest.raises(HardeningError, match="needs Linux"):
        make_non_dumpable()


def test_real_process_becomes_non_dumpable() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("prctl is Linux only")
    libc = ctypes.CDLL(None, use_errno=True)
    make_non_dumpable()
    assert libc.prctl(3, 0, 0, 0, 0) == 0
