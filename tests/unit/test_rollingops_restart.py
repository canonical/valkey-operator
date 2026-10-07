#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for restarts coordinated through charmlibs-rollingops."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from charmlibs.rollingops import OperationResult
from ops import testing

from common.exceptions import ValkeyWorkloadCommandError
from src.charm import ValkeyCharm
from src.literals import PEER_RELATION, ROLLINGOPS_PEER_RELATION, StartState

CONTAINER = "valkey"


def _state(unit_data=None, app_data=None, deferred=()):
    relations = {
        testing.PeerRelation(
            id=1,
            endpoint=PEER_RELATION,
            local_unit_data={"start-state": StartState.STARTED.value} | (unit_data or {}),
            local_app_data=app_data or {},
        ),
        testing.PeerRelation(id=2, endpoint=ROLLINGOPS_PEER_RELATION),
    }
    return testing.State(
        leader=True,
        relations=relations,
        containers={testing.Container(name=CONTAINER, can_connect=True)},
        deferred=list(deferred),
    )


@contextmanager
def _charm(state=None):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    with ctx(ctx.on.update_status(), state or _state()) as manager:
        charm = manager.charm
        charm.workload.restart = MagicMock()
        yield charm


def test_restart_callback_is_registered():
    """The lock-granted hook can find the restart callback."""
    with _charm() as charm:
        callbacks = charm.rollingops._peer_backend.callback_targets
        assert callbacks["restart"] == charm.base_events.restart_workload


@pytest.mark.parametrize(
    "unit_data,app_data",
    [({"backup-id": "b1"}, None), (None, {"restore-id": "r1"})],
)
def test_restart_callback_retries_during_backup_or_restore(unit_data, app_data):
    """A restart must not touch the workload while a backup or restore runs."""
    with _charm(_state(unit_data=unit_data, app_data=app_data)) as charm:
        assert charm.base_events.restart_workload() == OperationResult.RETRY_RELEASE
        charm.workload.restart.assert_not_called()


def test_restart_callback_retries_when_restart_fails():
    with _charm() as charm:
        charm.workload.restart.side_effect = ValkeyWorkloadCommandError("boom")
        assert (
            charm.base_events.restart_workload(restart_valkey=True, restart_sentinel=False)
            == OperationResult.RETRY_RELEASE
        )


def test_restart_callback_retries_when_valkey_unhealthy():
    with (
        _charm() as charm,
        patch("managers.cluster.ClusterManager.is_healthy", return_value=False),
        patch("core.models.ValkeyServer.update") as update,
    ):
        assert (
            charm.base_events.restart_workload(restart_valkey=True, restart_sentinel=False)
            == OperationResult.RETRY_RELEASE
        )
        update.assert_called_once_with({"is_valkey_healthy": False})


def test_restart_callback_retries_when_sentinel_unhealthy():
    with (
        _charm() as charm,
        patch("managers.sentinel.SentinelManager.restart_service"),
        patch("managers.sentinel.SentinelManager.is_healthy", return_value=False),
        patch("core.models.ValkeyServer.update") as update,
    ):
        assert (
            charm.base_events.restart_workload(restart_valkey=False, restart_sentinel=True)
            == OperationResult.RETRY_RELEASE
        )
        update.assert_called_with({"is_sentinel_healthy": False})


def test_restart_callback_releases_on_success():
    with (
        _charm() as charm,
        patch("managers.sentinel.SentinelManager.restart_service") as restart_sentinel,
        patch("managers.sentinel.SentinelManager.is_healthy", return_value=True),
        patch("managers.cluster.ClusterManager.is_healthy", return_value=True),
        patch("managers.cluster.ClusterManager.reconcile_min_replicas_to_write"),
        patch("core.models.ValkeyServer.update") as update,
    ):
        assert charm.base_events.restart_workload() == OperationResult.RELEASE
        charm.workload.restart.assert_called_once()
        restart_sentinel.assert_called_once()
        update.assert_any_call({"is_valkey_healthy": True})
        update.assert_any_call({"is_sentinel_healthy": True})


def test_restart_callback_rewrites_sentinel_config_for_primary_endpoint():
    with (
        _charm() as charm,
        patch("managers.config.ConfigManager.set_sentinel_config_properties") as write_config,
        patch("managers.sentinel.SentinelManager.restart_service"),
        patch("managers.sentinel.SentinelManager.is_healthy", return_value=True),
    ):
        charm.base_events.restart_workload(
            restart_valkey=False, restart_sentinel=True, primary_endpoint="10.0.0.1"
        )
        write_config.assert_called_once_with(primary_endpoint="10.0.0.1")


def test_deferred_restart_workload_from_previous_revision_is_dropped():
    """An event deferred by the previous revision must not crash the new one.

    The refresh restarts the services anyway, so dropping it loses nothing.
    """
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    deferred = testing.DeferredEvent(
        handle_path="ValkeyCharm/on/restart_workload[1]",
        owner="on",
        observer="_on_restart_workload",
    )
    state_out = ctx.run(ctx.on.update_status(), _state(deferred=[deferred]))
    assert not [d for d in state_out.deferred if "restart_workload" in d.handle_path]
