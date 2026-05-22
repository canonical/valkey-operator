#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import PropertyMock, patch

import pytest
from ops import testing

from charm import ValkeyCharm
from common.exceptions import ValkeyCannotGetPrimaryIPError, ValkeyWorkloadCommandError
from literals import CONTAINER, PEER_RELATION, ScaleDownState
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


def test_full_app_removal_does_not_wedge_when_lock_unavailable(cloud_spec):
    """On full-app removal a unit that can't get the scale-down lock goes away, not error.

    Removing the whole application fires storage-detaching on every unit at once; the
    losers of the lock race must not raise (which would wedge them in error state with a
    lost agent and block the application from ever being removed).
    """
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
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch(
            "managers.sentinel.SentinelManager.get_primary_ip_for_scale_down",
            return_value="10.0.1.1",
        ),
        patch("common.locks.ScaleDownLock.request_lock", return_value=False),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("core.models.ValkeyCluster.update"),
        patch("ops.model.Application.planned_units", return_value=0),
    ):
        # Must complete without raising RequestingLockTimedOutError.
        state_out = ctx.run(ctx.on.storage_detaching(data_storage), state_in)

    mock_stop.assert_not_called()
    out_relation = state_out.get_relations(PEER_RELATION)[0]
    assert out_relation.local_unit_data["scale-down-state"] == ScaleDownState.GOING_AWAY.value


def test_storage_detaching_goes_away_if_primary_lost_acquiring_lock(cloud_spec):
    """On full-app removal, a primary loss while acquiring the lock goes away, not error.

    request_lock re-reads the primary IP from sentinels; during full-app removal that can
    raise ValkeyCannotGetPrimaryIPError, which must be tolerated (go away) rather than
    wedging the unit in error. (Partial scale-down re-raises instead; see
    test_partial_scale_down_does_not_go_away_if_primary_lost.)
    """
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
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch(
            "managers.sentinel.SentinelManager.get_primary_ip_for_scale_down",
            return_value="10.0.1.1",
        ),
        patch(
            "common.locks.ScaleDownLock.request_lock",
            side_effect=ValkeyCannotGetPrimaryIPError("primary gone"),
        ),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("core.models.ValkeyCluster.update"),
        patch("ops.model.Application.planned_units", return_value=0),
    ):
        # Must complete without raising.
        state_out = ctx.run(ctx.on.storage_detaching(data_storage), state_in)

    mock_stop.assert_not_called()
    out_relation = state_out.get_relations(PEER_RELATION)[0]
    assert out_relation.local_unit_data["scale-down-state"] == ScaleDownState.GOING_AWAY.value


def test_partial_scale_down_does_not_go_away_if_primary_lost(cloud_spec):
    """A primary loss during a PARTIAL scale-down must raise, not silently go away.

    Only full-app removal (planned_units() == 0) may go away without the lock. During a
    single-unit scale-down the unit must surface the error so juju retries the hook, rather
    than tearing itself down without coordinating failover and the data save.
    """
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
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch(
            "managers.sentinel.SentinelManager.get_primary_ip_for_scale_down",
            return_value="10.0.1.1",
        ),
        patch(
            "common.locks.ScaleDownLock.request_lock",
            side_effect=ValkeyCannotGetPrimaryIPError("primary gone"),
        ),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("core.models.ValkeyCluster.update"),
        patch("ops.model.Application.planned_units", return_value=2),
    ):
        with pytest.raises(testing.errors.UncaughtCharmError):
            ctx.run(ctx.on.storage_detaching(data_storage), state_in)

    mock_stop.assert_not_called()


def test_full_app_removal_goes_away_if_sentinel_query_fails(cloud_spec):
    """On full-app removal a sentinel/workload error while acquiring the lock must not wedge.

    request_lock can raise ValkeyWorkloadCommandError (e.g. the sentinel query in
    get_active_sentinel_ips fails during teardown), not just ValkeyCannotGetPrimaryIPError.
    On full-app removal that must be tolerated by going away rather than parking the unit in
    error and blocking the application's removal.
    """
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
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch(
            "managers.sentinel.SentinelManager.get_primary_ip_for_scale_down",
            return_value="10.0.1.1",
        ),
        patch(
            "common.locks.ScaleDownLock.request_lock",
            side_effect=ValkeyWorkloadCommandError("sentinel query failed"),
        ),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("core.models.ValkeyCluster.update"),
        patch("ops.model.Application.planned_units", return_value=0),
    ):
        # Must complete without raising.
        state_out = ctx.run(ctx.on.storage_detaching(data_storage), state_in)

    mock_stop.assert_not_called()
    out_relation = state_out.get_relations(PEER_RELATION)[0]
    assert out_relation.local_unit_data["scale-down-state"] == ScaleDownState.GOING_AWAY.value


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
        patch("common.client.ValkeyClient.role") as get_replica_offset,
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
        assert get_replica_offset.call_count == 2
        save_dataset.assert_called_once()
        status_is(state_out, ScaleDownStatuses.GOING_AWAY.value)


def test_non_primary_block_until_synced(cloud_spec):
    """Test scale-down behavior when this unit is not the primary but needs sync before shutdown."""
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
        patch(
            "common.client.ValkeyClient.role",
            side_effect=[
                ["master", 1108321968, ["valkey-0.valkey-endpoints", "6380", "1108321473"]],
                ["slave", "valkey-1.valkey-endpoints", 6380, "connected", 1108321473],
                ["slave", "valkey-1.valkey-endpoints", 6380, "connected", 1108321968],
            ],
        ) as get_replica_offset,
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
        assert get_replica_offset.call_count == 3
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
        patch("common.client.ValkeyClient.role") as get_replica_offset,
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
        get_replica_offset.assert_not_called()
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
