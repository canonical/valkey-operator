#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for ClusterManager.reconcile_min_replicas_to_write."""

from unittest.mock import MagicMock

import pytest

from common.exceptions import ValkeyConfigSetError
from managers.cluster import ClusterManager


def _make_cluster_manager(planned_units: int, config_set_ok: bool = True):
    """Build a ClusterManager with a mocked Valkey client.

    Returns the manager and the mock client so callers can assert on the
    config_set call.
    """
    state = MagicMock()
    state.endpoint = "10.0.0.5"
    state.charm.app.planned_units.return_value = planned_units

    cm = ClusterManager(state=state, workload=MagicMock())
    client = MagicMock()
    client.config_set.return_value = config_set_ok
    cm._get_valkey_client = MagicMock(return_value=client)
    return cm, client


@pytest.mark.parametrize(
    "planned_units,expected",
    [(0, "0"), (1, "0"), (2, "0"), (3, "1"), (5, "1")],
)
def test_reconcile_sets_value_per_topology(planned_units, expected):
    """min-replicas-to-write is '1' only when the cluster can lose a replica (>= 3 units)."""
    cm, client = _make_cluster_manager(planned_units)

    cm.reconcile_min_replicas_to_write()

    client.config_set.assert_called_once_with(
        hostname="10.0.0.5",
        parameter="min-replicas-to-write",
        value=expected,
    )


def test_reconcile_raises_when_config_set_fails():
    """A failed CONFIG SET surfaces as ValkeyConfigSetError, like other setters."""
    cm, _ = _make_cluster_manager(planned_units=3, config_set_ok=False)

    with pytest.raises(ValkeyConfigSetError):
        cm.reconcile_min_replicas_to_write()
