#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import PropertyMock, patch

import pytest
from ops import testing

from charm import ValkeyCharm
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
            for unit_id in range(1, 4)
        },
    )


def test_other_unit_has_lock(cloud_spec):
    """Test that if another unit has the lock, then the lock is not acquired."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = get_3_unit_peer_relation()
    container = testing.Container(name=CONTAINER, can_connect=True)
    data_stroage = testing.Storage(name="data")
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
        storages={data_stroage},
    )

    with (
        patch("common.locks.ScaleDownLock.request_lock", return_value=False),
    ):
        # expect raised exception due to lock not being acquired
        with pytest.raises(testing.errors.UncaughtCharmError) as exc_info:
            ctx.run(ctx.on.storage_detaching(data_stroage), state_in)
        assert "RequestingLockTimedOutError" in str(exc_info.value)


def test_non_primary(cloud_spec):
    """Test that if another unit has the lock, then the lock is not acquired."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = get_3_unit_peer_relation()
    container = testing.Container(name=CONTAINER, can_connect=True)
    data_stroage = testing.Storage(name="data")
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
        storages={data_stroage},
    )

    with (
        patch("common.locks.ScaleDownLock.request_lock", return_value=True),
        patch("common.locks.ScaleDownLock.release_lock", return_value=True),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.1.1"),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch(
            "managers.sentinel.SentinelManager.reset_sentinel_states"
        ) as mock_reset_sentinel_states,
        patch(
            "managers.sentinel.SentinelManager.verify_expected_replica_count"
        ) as mock_verify_expected_replica_count,
        patch(
            "managers.sentinel.SentinelManager.get_active_sentinel_ips",
            return_value=["10.0.1.1", "10.0.1.2", "10.0.1.3"],
        ),
    ):
        state_out = ctx.run(ctx.on.storage_detaching(data_stroage), state_in)
        mock_stop.assert_called_once()
        mock_reset_sentinel_states.assert_called_once()
        mock_verify_expected_replica_count.assert_called_once()
        status_is(state_out, ScaleDownStatuses.GOING_AWAY.value)


def test_primary(cloud_spec):
    """Test that if another unit has the lock, then the lock is not acquired."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = get_3_unit_peer_relation()
    container = testing.Container(name=CONTAINER, can_connect=True)
    data_stroage = testing.Storage(name="data")
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
        storages={data_stroage},
    )

    with (
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock(return_value="10.0.1.0"),
        ),
        patch("common.locks.ScaleDownLock.request_lock", return_value=True),
        patch("common.locks.ScaleDownLock.release_lock", return_value=True),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.1.0"),
        patch("workload_k8s.ValkeyK8sWorkload.stop") as mock_stop,
        patch("managers.sentinel.SentinelManager.failover") as mock_failover,
        patch(
            "managers.sentinel.SentinelManager.reset_sentinel_states"
        ) as mock_reset_sentinel_states,
        patch(
            "managers.sentinel.SentinelManager.verify_expected_replica_count"
        ) as mock_verify_expected_replica_count,
        patch(
            "managers.sentinel.SentinelManager.get_active_sentinel_ips",
            return_value=["10.0.1.1", "10.0.1.2", "10.0.1.3"],
        ),
    ):
        state_out = ctx.run(ctx.on.storage_detaching(data_stroage), state_in)
        mock_failover.assert_called_once()
        mock_stop.assert_called_once()
        mock_reset_sentinel_states.assert_called_once()
        mock_verify_expected_replica_count.assert_called_once()
        status_is(state_out, ScaleDownStatuses.GOING_AWAY.value)
