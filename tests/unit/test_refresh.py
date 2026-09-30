#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from charm_refresh import CharmVersion, PrecheckFailed
from ops import BlockedStatus, testing

from charm import ValkeyCharm
from common.exceptions import ValkeyCannotGetPrimaryIPError
from literals import (
    PEER_RELATION,
)
from src.events.refresh import K8sValkeyRefresh, MachinesValkeyRefresh, ValkeyRefresh

CONTAINER = "valkey"

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


@pytest.mark.parametrize(
    "old_version, new_version, expected",
    [
        ("9.0.4", "9.0.5", True),  # Patch upgrade allowed
        ("9.0.4", "9.1.0", True),  # Minor upgrade allowed
        ("9.0.4", "10.0.0", False),  # Major upgrade not allowed
        ("9.1.0", "9.0.4", False),  # Downgrade not allowed
        ("9.0.5", "9.0.4", False),  # Downgrade not allowed
    ],
)
def test_is_workload_compatible(old_version: str, new_version: str, expected: bool) -> None:
    assert (
        ValkeyRefresh.is_compatible(
            old_charm_version=CharmVersion("9/1.1.0"),
            new_charm_version=CharmVersion("9/1.1.1"),
            old_workload_version=old_version,
            new_workload_version=new_version,
        )
        == expected
    )


@pytest.mark.parametrize(
    "app_data, unit_data, pre_check_result",
    [
        (
            {},
            {},
            "Unit is not started or being removed",
        ),
        (
            {},
            {"backup_id": "XYZ", "start-state": "started"},
            "Backup in progress, wait for completion",
        ),
        (
            {"restore_id": "XYZ"},
            {"start-state": "started"},
            "Database restore in progress, cannot upgrade",
        ),
        (
            {},
            {"start-state": "started", "tls_client_state": "to-tls"},
            "TLS switchover or CA rotation in progress, cannot upgrade",
        ),
        (
            {},
            {"start-state": "started", "tls_ca_rotation": "new-ca-detected"},
            "TLS switchover or CA rotation in progress, cannot upgrade",
        ),
    ],
)
def test_pre_refresh_checks(app_data, unit_data, pre_check_result) -> None:
    ctx = testing.Context(ValkeyCharm, app_trusted=True)

    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_app_data=app_data,
        local_unit_data=unit_data,
    )

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        relations={peer_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )

    with ctx(ctx.on.relation_changed(relation=peer_relation, remote_unit=1), state_in) as manager:
        charm: ValkeyCharm = manager.charm

        # Mock the refresh constructor to avoid version checks
        with (
            patch("events.refresh.ValkeyRefresh.__init__", return_value=None),
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
        ):
            refresh = K8sValkeyRefresh.__new__(K8sValkeyRefresh)
            refresh.charm = charm
            with pytest.raises(PrecheckFailed) as e:
                refresh.run_pre_refresh_checks_after_1_unit_refreshed()

            assert str(e.value) == pre_check_result


def test_pre_refresh_check_primary_unavailable(vm_environment) -> None:
    ctx = testing.Context(ValkeyCharm)

    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )

    state_in = testing.State(
        relations={peer_relation},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )

    with ctx(ctx.on.relation_created(relation=peer_relation), state_in) as manager:
        charm: ValkeyCharm = manager.charm

        # Mock the refresh constructor to avoid version checks
        with (
            patch("events.refresh.ValkeyRefresh.__init__", return_value=None),
            patch(
                "managers.sentinel.SentinelManager.get_primary_ip",
                side_effect=ValkeyCannotGetPrimaryIPError("error"),
            ),
        ):
            refresh = MachinesValkeyRefresh.__new__(MachinesValkeyRefresh)
            refresh.charm = charm
            with pytest.raises(PrecheckFailed) as e:
                refresh.run_pre_refresh_checks_before_any_units_refreshed()

            assert str(e.value) == "Primary not available, cannot upgrade"


def test_snap_refresh_failed(vm_environment) -> None:
    ctx = testing.Context(ValkeyCharm)

    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )

    state_in = testing.State(
        relations={peer_relation},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )

    with ctx(ctx.on.relation_created(relation=peer_relation), state_in) as manager:
        mock_refresh = MagicMock()
        mock_refresh.next_unit_allowed_to_refresh = False
        charm: ValkeyCharm = manager.charm

        # Mock the refresh constructor to avoid version checks
        with (
            patch("events.refresh.ValkeyRefresh.__init__", return_value=None),
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
            patch("managers.sentinel.SentinelManager.get_active_sentinel_ips"),
            patch("managers.sentinel.SentinelManager.failover"),
            patch("workload_vm.ValkeyVmWorkload.stop"),
            patch("workload_vm.ValkeyVmWorkload.install", return_value=False),
            patch("workload_vm.ValkeyVmWorkload.snap_revision", return_value="182"),
            patch("workload_vm.ValkeyVmWorkload.start"),
            patch("managers.auth.AuthManager.configure_auth"),
            patch("managers.config.ConfigManager.configure_services"),
            patch("managers.cluster.ClusterManager.is_healthy"),
            patch("managers.sentinel.SentinelManager.is_healthy"),
        ):
            refresh = MachinesValkeyRefresh.__new__(MachinesValkeyRefresh)
            refresh.charm = charm
            refresh.refresh_snap(
                snap_name="valkey-charmed", snap_revision="183", refresh=mock_refresh
            )

            assert not mock_refresh.next_unit_allowed_to_refresh


def test_snap_refresh_successful(vm_environment) -> None:
    ctx = testing.Context(ValkeyCharm)

    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )

    state_in = testing.State(
        relations={peer_relation},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )

    with ctx(ctx.on.relation_created(relation=peer_relation), state_in) as manager:
        mock_refresh = MagicMock()
        mock_refresh.next_unit_allowed_to_refresh = False
        charm: ValkeyCharm = manager.charm

        # Mock the refresh constructor to avoid version checks
        with (
            patch("events.refresh.ValkeyRefresh.__init__", return_value=None),
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
            patch("managers.sentinel.SentinelManager.get_active_sentinel_ips"),
            patch("managers.sentinel.SentinelManager.failover"),
            patch("workload_vm.ValkeyVmWorkload.stop"),
            patch("workload_vm.ValkeyVmWorkload.install", return_value=True),
            patch("workload_vm.ValkeyVmWorkload.snap_revision", return_value="182"),
            patch("workload_vm.ValkeyVmWorkload.start"),
        ):
            refresh = MachinesValkeyRefresh.__new__(MachinesValkeyRefresh)
            refresh.charm = charm
            refresh.charm.post_refresh_handling = MagicMock()
            refresh.refresh_snap(
                snap_name="valkey-charmed", snap_revision="183", refresh=mock_refresh
            )

            refresh.charm.post_refresh_handling.assert_called_once()


def test_post_refresh_healthy_cluster() -> None:
    ctx = testing.Context(ValkeyCharm, app_trusted=True)

    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        relations={peer_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )

    with (
        patch("managers.sentinel.SentinelManager.get_primary_ip"),
        patch("managers.sentinel.SentinelManager.get_active_sentinel_ips"),
        patch("workload_k8s.ValkeyK8sWorkload.stop"),
        patch("workload_k8s.ValkeyK8sWorkload.start"),
        patch("workload_k8s.ValkeyK8sWorkload.alive"),
        patch("managers.auth.AuthManager.configure_auth"),
        patch("managers.config.ConfigManager.configure_services"),
        patch("managers.metrics.MetricsManager.reconcile"),
        patch("managers.cluster.ClusterManager.is_healthy", return_value=True),
        patch("managers.sentinel.SentinelManager.is_healthy", return_value=True),
    ):
        with ctx(ctx.on.relation_created(relation=peer_relation), state_in) as manager:
            mock_refresh = MagicMock()
            mock_refresh.next_unit_allowed_to_refresh = False
            charm: ValkeyCharm = manager.charm

            charm.refresh = mock_refresh
            charm.post_refresh_handling()

            assert mock_refresh.next_unit_allowed_to_refresh


def test_post_refresh_unhealthy_cluster() -> None:
    ctx = testing.Context(ValkeyCharm, app_trusted=True)

    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        relations={peer_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )

    with (
        patch("managers.sentinel.SentinelManager.get_primary_ip"),
        patch("managers.sentinel.SentinelManager.get_active_sentinel_ips"),
        patch("workload_k8s.ValkeyK8sWorkload.stop"),
        patch("workload_k8s.ValkeyK8sWorkload.start"),
        patch("workload_k8s.ValkeyK8sWorkload.alive"),
        patch("managers.auth.AuthManager.configure_auth"),
        patch("managers.config.ConfigManager.configure_services"),
        patch("managers.metrics.MetricsManager.reconcile"),
        patch("managers.cluster.ClusterManager.is_healthy", return_value=True),
        patch("managers.sentinel.SentinelManager.is_healthy", return_value=False),
    ):
        with ctx(ctx.on.relation_created(relation=peer_relation), state_in) as manager:
            mock_refresh = MagicMock()
            mock_refresh.next_unit_allowed_to_refresh = False
            charm: ValkeyCharm = manager.charm

            charm.refresh = mock_refresh
            charm.post_refresh_handling()

            assert not mock_refresh.next_unit_allowed_to_refresh


def test_statuses() -> None:
    ctx = testing.Context(ValkeyCharm, app_trusted=True)

    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )

    # higher app status
    refresh_mock = MagicMock()
    refresh_mock.app_status_higher_priority = BlockedStatus("123")
    refresh_mock.unit_status_higher_priority = None
    refresh_mock.unit_status_lower_priority.return_value = False
    with patch("charm_refresh.Kubernetes", MagicMock(return_value=refresh_mock)):
        state_out = ctx.run(ctx.on.update_status(), state_in)

        assert state_out.app_status == BlockedStatus("123")
        assert state_out.unit_status != BlockedStatus("123")

    # higher unit status
    refresh_mock = MagicMock()
    refresh_mock.app_status_higher_priority = None
    refresh_mock.unit_status_higher_priority = BlockedStatus("456")
    refresh_mock.unit_status_lower_priority.return_value = False
    with patch("charm_refresh.Kubernetes", MagicMock(return_value=refresh_mock)):
        state_out = ctx.run(ctx.on.update_status(), state_in)

        assert state_out.unit_status == BlockedStatus("456")

    # lower unit status
    refresh_mock = MagicMock()
    refresh_mock.app_status_higher_priority = None
    refresh_mock.unit_status_higher_priority = None
    refresh_mock.unit_status_lower_priority.return_value = BlockedStatus("789")
    with patch("charm_refresh.Kubernetes", MagicMock(return_value=refresh_mock)):
        state_out = ctx.run(ctx.on.update_status(), state_in)

        assert state_out.unit_status != BlockedStatus("789")

    # invalid status - this must raise to avoid downtime because of overridden refresh-status
    refresh_mock = MagicMock()
    refresh_mock.app_status_higher_priority = "invalid_status"
    refresh_mock.unit_status_higher_priority = None
    refresh_mock.unit_status_lower_priority.return_value = False
    with patch("charm_refresh.Kubernetes", MagicMock(return_value=refresh_mock)):
        with pytest.raises(testing.errors.UncaughtCharmError) as e:
            ctx.run(ctx.on.update_status(), state_in)

        assert isinstance(e.value.__cause__, AttributeError)
