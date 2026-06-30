#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the logs/archive storage volumes."""

from unittest.mock import MagicMock, patch

from ops import testing

from literals import PEER_RELATION, STATUS_PEERS_RELATION
from src.charm import ValkeyCharm
from workload_k8s import ValkeyK8sWorkload
from workload_vm import ValkeyVmWorkload

CONTAINER = "valkey"


def _base_relations():
    return {
        testing.PeerRelation(id=1, endpoint=PEER_RELATION),
        testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION),
    }


def test_k8s_workload_exposes_log_and_archive_dirs():
    wl = ValkeyK8sWorkload(MagicMock())
    assert wl.log_dir.as_posix() == "/var/log/valkey"
    assert wl.archive_dir.as_posix() == "/var/backups/valkey"


def test_vm_workload_exposes_log_and_archive_dirs():
    with patch("workload_vm.snap.SnapCache"):
        wl = ValkeyVmWorkload()
    assert wl.log_dir.as_posix() == "/var/snap/charmed-valkey/common/var/log/charmed-valkey"
    assert wl.archive_dir.as_posix() == "/var/snap/charmed-valkey/common/archive"


def test_storage_attached_logs_chowns_and_chmods_on_k8s(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    logs = testing.Storage(name="logs")
    state_in = testing.State(
        model=testing.Model(name="m", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        containers={testing.Container(name=CONTAINER, can_connect=True)},
        storages={logs},
        relations=_base_relations(),
    )
    with patch("workload_k8s.ValkeyK8sWorkload.exec") as mock_exec:
        ctx.run(ctx.on.storage_attached(logs), state_in)
    mock_exec.assert_any_call(["chown", "-R", "_daemon_:_daemon_", "/var/log/valkey"])
    mock_exec.assert_any_call(["chmod", "-R", "750", "/var/log/valkey"])


def test_storage_attached_archive_targets_archive_dir_on_k8s(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    archive = testing.Storage(name="archive")
    state_in = testing.State(
        model=testing.Model(name="m", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        containers={testing.Container(name=CONTAINER, can_connect=True)},
        storages={archive},
        relations=_base_relations(),
    )
    with patch("workload_k8s.ValkeyK8sWorkload.exec") as mock_exec:
        ctx.run(ctx.on.storage_attached(archive), state_in)
    mock_exec.assert_any_call(["chmod", "-R", "750", "/var/backups/valkey"])


def test_storage_attached_logs_chmods_only_on_vm(cloud_spec_vm):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    logs = testing.Storage(name="logs")
    state_in = testing.State(
        model=testing.Model(name="m", type="lxd", cloud_spec=cloud_spec_vm),
        leader=True,
        storages={logs},
        relations=_base_relations(),
    )
    with (
        patch("workload_vm.snap.SnapCache"),  # avoid flaky snap-store lookup in __init__
        patch("workload_vm.ValkeyVmWorkload.exec") as mock_exec,
    ):
        ctx.run(ctx.on.storage_attached(logs), state_in)
    log_path = "/var/snap/charmed-valkey/common/var/log/charmed-valkey"
    mock_exec.assert_any_call(["chmod", "-R", "750", log_path])
    chown_calls = [c for c in mock_exec.call_args_list if c.args and c.args[0][0] == "chown"]
    assert chown_calls == [], "VM must not chown — valkey runs as root"
