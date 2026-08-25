#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for ClusterManager."""

from unittest.mock import MagicMock

import pytest
import tenacity

from common.exceptions import ValkeyClusterNotReadyError, ValkeyWorkloadCommandError
from managers.cluster import ClusterManager


def _make_cluster_manager(active_units: int, inactive_units: int = 1, config_set_ok: bool = True):
    """Build a ClusterManager with a mocked Valkey client.

    `active_units` servers report is_active=True; `inactive_units` report
    False so the active count, not the total, is what drives the decision.
    Returns the manager and the mock client so callers can assert on the
    config_set call.
    """
    state = MagicMock()
    state.endpoint = "10.0.0.5"
    active = [MagicMock(is_active=True) for _ in range(active_units)]
    inactive = [MagicMock(is_active=False) for _ in range(inactive_units)]
    state.servers = active + inactive

    cm = ClusterManager(state=state, workload=MagicMock())
    client = MagicMock()
    if not config_set_ok:
        client.config_set.side_effect = ValkeyWorkloadCommandError("failed")
    cm._get_valkey_client = MagicMock(return_value=client)
    return cm, client


@pytest.mark.parametrize(
    "active_units,expected",
    [(0, "0"), (1, "0"), (2, "0"), (3, "1"), (5, "1")],
)
def test_reconcile_sets_value_per_topology(active_units, expected):
    """min-replicas-to-write is '1' only when >= 3 units are currently active."""
    cm, client = _make_cluster_manager(active_units)

    cm.reconcile_min_replicas_to_write()

    client.config_set.assert_called_once_with(
        hostname="10.0.0.5",
        config_settings={"min-replicas-to-write": expected},
    )


def test_reconcile_swallows_config_set_failure(caplog):
    """A failed CONFIG SET is logged and swallowed, not raised.

    The value is non-critical and gets reasserted on the next event or restart,
    so a transient failure must not propagate out of the manager.
    """
    cm, client = _make_cluster_manager(active_units=3, config_set_ok=False)

    cm.reconcile_min_replicas_to_write()

    client.config_set.assert_called_once()
    assert "Failed to reconcile min-replicas-to-write" in caplog.text


def _cluster_manager_with_client():
    """Build a ClusterManager whose _get_valkey_client returns a fresh mock client."""
    state = MagicMock()
    state.endpoint = "10.0.0.5"
    cm = ClusterManager(state=state, workload=MagicMock())
    client = MagicMock()
    cm._get_valkey_client = MagicMock(return_value=client)
    return cm, client


@pytest.mark.parametrize(
    "role_first,expected",
    [("master", True), ("slave", False)],
)
def test_is_primary_reads_local_role(role_first, expected):
    """is_primary is True only when the local server reports the master role."""
    cm, client = _cluster_manager_with_client()
    client.role.return_value = [role_first, "0", []]

    assert cm.is_primary() is expected
    client.role.assert_called_once_with(hostname="10.0.0.5")


def test_wait_until_loaded_times_out_raises_not_ready(mocker):
    """wait_until_loaded raises ValkeyClusterNotReadyError when ping never succeeds."""
    cm, client = _cluster_manager_with_client()
    client.ping.return_value = False
    # Collapse the bounded retry so the test doesn't actually wait.
    mocker.patch("managers.cluster.stop_after_delay", return_value=tenacity.stop_after_attempt(2))
    mocker.patch("managers.cluster.wait_fixed", return_value=tenacity.wait_none())

    with pytest.raises(ValkeyClusterNotReadyError):
        cm.wait_until_loaded(600)


def test_wait_until_resynced_times_out_raises_not_ready(mocker):
    """wait_until_resynced raises ValkeyClusterNotReadyError when the replica never syncs."""
    cm, _ = _cluster_manager_with_client()
    cm.is_replica_synced = MagicMock(return_value=False)
    mocker.patch("managers.cluster.stop_after_delay", return_value=tenacity.stop_after_attempt(2))
    mocker.patch("managers.cluster.wait_fixed", return_value=tenacity.wait_none())

    with pytest.raises(ValkeyClusterNotReadyError):
        cm.wait_until_resynced(900)


def test_bounded_waits_log_their_own_progress(mocker, caplog):
    """The restore waits log where they happen -- in the manager, not the event handler.

    Log output belongs in the manager method that does the work, so the event
    flow stays readable.
    """
    import logging

    cm, client = _cluster_manager_with_client()
    client.ping.return_value = True
    client.info_persistence.return_value = {"loading": "0"}
    cm.is_replica_synced = MagicMock(return_value=True)

    with caplog.at_level(logging.INFO):
        cm.wait_until_loaded(600)
        cm.wait_until_resynced(900)

    assert "restore.wait: dataset to load (up to 600s)" in caplog.text
    assert "restore.wait: dataset loaded" in caplog.text
    assert "restore.wait: replica resync (up to 900s)" in caplog.text
    assert "restore.wait: replica resynced" in caplog.text
