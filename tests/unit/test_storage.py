#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the logs/archive storage volumes."""

from unittest.mock import MagicMock, patch

from workload_k8s import ValkeyK8sWorkload
from workload_vm import ValkeyVmWorkload


def test_k8s_workload_exposes_log_and_archive_dirs():
    wl = ValkeyK8sWorkload(MagicMock())
    assert wl.log_dir.as_posix() == "/var/log/valkey"
    assert wl.archive_dir.as_posix() == "/var/backups/valkey"


def test_vm_workload_exposes_log_and_archive_dirs():
    with patch("workload_vm.snap.SnapCache"):
        wl = ValkeyVmWorkload()
    assert wl.log_dir.as_posix() == "/var/snap/charmed-valkey/common/var/log/charmed-valkey"
    assert wl.archive_dir.as_posix() == "/var/snap/charmed-valkey/common/archive"
