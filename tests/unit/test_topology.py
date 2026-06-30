#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for TopologyManager observer-log path selection + fallback."""

from pathlib import PurePosixPath
from unittest.mock import MagicMock

from literals import Substrate
from managers.topology import TopologyManager


def _topology_manager(substrate, log_dir):
    tm = object.__new__(TopologyManager)
    tm.state = MagicMock()
    tm.state.substrate = substrate
    tm.workload = MagicMock()
    tm.workload.log_dir = PurePosixPath(log_dir)
    return tm


def test_observer_log_uses_log_dir_on_vm(tmp_path):
    tm = _topology_manager(Substrate.VM, str(tmp_path))  # parent exists
    stream = tm._open_observer_log()
    try:
        assert stream.name == (tmp_path / "topology_observer.log").as_posix()
    finally:
        stream.close()


def test_observer_log_falls_back_when_parent_missing(tmp_path, mocker):
    fallback = tmp_path / "fallback.log"
    mocker.patch("managers.topology.TOPOLOGY_OBSERVER_LOG_FILE", fallback.as_posix())
    tm = _topology_manager(Substrate.VM, str(tmp_path / "missing"))  # parent absent
    stream = tm._open_observer_log()
    try:
        assert stream.name == fallback.as_posix()
    finally:
        stream.close()
