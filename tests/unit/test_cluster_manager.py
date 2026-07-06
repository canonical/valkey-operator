#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for ClusterManager.reconcile_min_replicas_to_write."""

from unittest.mock import MagicMock

import pytest

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
    client.config_set.return_value = config_set_ok
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
        parameter="min-replicas-to-write",
        value=expected,
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
