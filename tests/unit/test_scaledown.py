#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import PropertyMock, patch

import pytest
from ops import testing

from charm import ValkeyCharm
from common.exceptions import ValkeyCannotGetPrimaryIPError, ValkeyWorkloadCommandError
from literals import CONTAINER, PEER_RELATION
from statuses import ScaleDownStatuses
from tests.unit.helpers import status_is


def get_3_unit_peer_relation():
    return testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "hostname": "valkey-0",
            "private-ip": "10.0.1.0",
            "start-state": "started",
        },
        peers_data={
            unit_id: {
                "hostname": f"valkey-{unit_id}",
                "private-ip": f"10.0.1.{unit_id}",
                "start-state": "started",
            }
            for unit_id in range(1, 3)
        },
    )


def test_other_unit_has_lock(cloud_spec):
    """Test that if another unit has the lock, then the lock is not acquired."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = get_3_unit_peer_relation()
    container = testing.Container(name=CONTAINER, can_connect=True)
    data_storage = testing.Storage(name="data")
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
        storages={data_storage},
    )

    with (
        patch("common.locks.ScaleDownLock.request_lock", return_value=False),
        patch(
            "common.client.SentinelClient.get_primary_addr_by_name",
            side_effect=[
                ValkeyWorkloadCommandError("errored out"),
                ("10.0.1.1", 6379),
            ],
        ),
    ):
        # expect raised exception due to lock not being acquired
        with pytest.raises(testing.errors.UncaughtCharmError) as exc_info:
            ctx.run(ctx.on.storage_detaching(data_storage), state_in)
        assert "RequestingLockTimedOutError" in str(exc_info.value)


def test_non_primary(cloud_spec):
    """Test scale-down behavior when this unit is not the primary but successfully acquires the lock."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = get_3_unit_peer_relation()
    container = testing.Container(name=CONTAINER, can_connect=True)
    data_strorage = testing.Storage(name="data")
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
        storages={data_strorage},
    )

    with (
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch("common.locks.ScaleDownLock.request_lock", return_value=True),
        patch("common.locks.ScaleDownLock.release_lock", return_value=True),
        patch(
            "common.client.SentinelClient.get_primary_addr_by_name",
            return_value=("valkey-1", 6379),
        ),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("common.client.SentinelClient.reset") as mock_reset,
        patch("common.client.ValkeyClient.save") as save_dataset,
        patch(
            "common.client.SentinelClient.sentinels_primary",
            side_effect=[
                [{"ip": "valkey-0"}, {"ip": "valkey-2"}],  # for get_active_sentinel_ips
                [{"ip": "valkey-2"}],  # for target_sees_all_others unit valkey-1
                [{"ip": "valkey-1"}],  # for target_sees_all_others unit valkey-2
            ],
        ),
        patch(
            "common.client.SentinelClient.replicas_primary", return_value=[{"ip": "ip"}]
        ),  # we need the len to be 1
    ):
        state_out = ctx.run(ctx.on.storage_detaching(data_strorage), state_in)
        mock_stop.assert_called_once()
        assert mock_reset.call_count == 2
        save_dataset.assert_called_once()
        status_is(state_out, ScaleDownStatuses.GOING_AWAY.value)


def test_primary(cloud_spec):
    """Test scale-down behavior when this unit is the primary and successfully acquires the lock."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = get_3_unit_peer_relation()
    container = testing.Container(name=CONTAINER, can_connect=True)
    data_strorage = testing.Storage(name="data")
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
        storages={data_strorage},
    )

    with (
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch("common.locks.ScaleDownLock.request_lock", return_value=True),
        patch("common.locks.ScaleDownLock.release_lock", return_value=True),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="valkey-0"),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("common.client.SentinelClient.failover_primary_coordinated") as mock_failover,
        patch(
            "common.client.SentinelClient.is_failover_in_progress", return_value=False
        ) as mock_failover_in_progress,
        patch("common.client.SentinelClient.reset") as mock_reset,
        patch("common.client.ValkeyClient.save") as save_dataset,
        patch(
            "common.client.SentinelClient.sentinels_primary",
            side_effect=[
                [{"ip": "10.0.1.1"}, {"ip": "10.0.1.2"}],  # for get_active_sentinel_ips
                [],  # for target_sees_all_others unit 10.0.1.1 not yet
                ValkeyWorkloadCommandError(
                    "errored out"
                ),  # for target_sees_all_others unit 10.0.1.1 network mishap
                [{"ip": "10.0.1.2"}],  # for target_sees_all_others unit 10.0.1.1
                [{"ip": "10.0.1.1"}],  # for target_sees_all_others unit 10.0.1.2
            ],
        ),
        patch(
            "common.client.SentinelClient.replicas_primary", return_value=[{"ip": "ip"}]
        ),  # we need the len to be 1
    ):
        state_out = ctx.run(ctx.on.storage_detaching(data_strorage), state_in)
        mock_failover.assert_called_once()
        mock_failover_in_progress.assert_called_once()
        mock_stop.assert_called_once()
        assert mock_reset.call_count == 2
        save_dataset.assert_called_once()
        status_is(state_out, ScaleDownStatuses.GOING_AWAY.value)


def test_last_leader_unit_going_down(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "hostname": "valkey-0",
            "private-ip": "10.0.1.0",
            "start-state": "started",
        },
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    data_strorage = testing.Storage(name="data")
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
        storages={data_strorage},
    )

    with (
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch("common.locks.ScaleDownLock.request_lock", return_value=True),
        patch("common.locks.ScaleDownLock.release_lock", return_value=True),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="valkey-0"),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("common.client.SentinelClient.sentinels_primary", return_value=[]),
        patch("common.client.ValkeyClient.save") as save_dataset,
        patch("core.models.ValkeyCluster.update") as cluster_update,
        patch("ops.model.Application.planned_units", return_value=0),
    ):
        state_out = ctx.run(ctx.on.storage_detaching(data_strorage), state_in)
        mock_stop.assert_called_once()
        save_dataset.assert_called_once()
        status_is(state_out, ScaleDownStatuses.GOING_AWAY.value)
        cluster_update.assert_called_once_with(
            {"internal_ca_certificate": None, "internal_ca_private_key": None}
        )


def test_cannot_get_primary_ip_leader(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "hostname": "valkey-0",
            "private-ip": "10.0.1.0",
            "start-state": "started",
        },
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    data_strorage = testing.Storage(name="data")
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
        storages={data_strorage},
    )

    with (
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch(
            "managers.sentinel.SentinelManager.get_primary_ip",
            side_effect=ValkeyCannotGetPrimaryIPError("errored out"),
        ),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("core.models.ValkeyCluster.update") as cluster_update,
        patch("ops.model.Application.planned_units", return_value=0),
    ):
        state_out = ctx.run(ctx.on.storage_detaching(data_strorage), state_in)
        mock_stop.assert_not_called()
        status_is(state_out, ScaleDownStatuses.GOING_AWAY.value)
        cluster_update.assert_called_once_with(
            {"internal_ca_certificate": None, "internal_ca_private_key": None}
        )
