#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the cluster operation locks."""

from unittest.mock import MagicMock, PropertyMock, patch

from common.locks import ScaleDownLock


def _make_charm(unit_name="valkey/0", primary_ip="10.0.1.0", active_sentinels=None):
    """Build a stub charm exposing only what ScaleDownLock touches."""
    charm = MagicMock()
    charm.app.name = "valkey"
    charm.state.unit_server.unit_name = unit_name
    charm.sentinel_manager.get_primary_ip.return_value = primary_ip
    charm.sentinel_manager.get_primary_ip_for_scale_down.return_value = primary_ip
    charm.sentinel_manager.get_active_sentinel_ips.return_value = (
        active_sentinels if active_sentinels is not None else ["10.0.1.0", "10.0.1.1"]
    )
    return charm


def test_request_lock_retries_until_lock_frees():
    """request_lock keeps polling SET NX until a contended lock frees, then acquires it."""
    lock = ScaleDownLock(_make_charm())

    with (
        patch.object(ScaleDownLock, "client", new_callable=PropertyMock) as mock_client_prop,
        patch("common.locks.time.sleep") as mock_sleep,
    ):
        client = mock_client_prop.return_value
        client.get.return_value = None  # nobody currently holds the lock key
        client.set.side_effect = [None, "OK"]  # peer holds it, then it frees

        acquired = lock.request_lock(timeout=10)

    assert acquired is True
    assert client.set.call_count == 2
    mock_sleep.assert_called_once()  # waited once between the two attempts


def test_request_lock_uses_resilient_primary_lookup_on_retry():
    """On retry the lock re-resolves the primary via the resilient scale-down lookup.

    get_primary_ip_for_scale_down rides out the transient sentinel failures common during
    teardown; the bare get_primary_ip does not. Attempt 0 reuses the caller-provided
    primary_ip (no lookup), and only the retry re-resolves it.
    """
    charm = _make_charm()
    lock = ScaleDownLock(charm)

    with (
        patch.object(ScaleDownLock, "client", new_callable=PropertyMock) as mock_client_prop,
        patch("common.locks.time.sleep"),
    ):
        client = mock_client_prop.return_value
        client.get.return_value = None  # nobody currently holds the lock key
        client.set.side_effect = [None, "OK"]  # contended once, then frees

        assert lock.request_lock(timeout=10, primary_ip="10.0.1.0") is True

    charm.sentinel_manager.get_primary_ip_for_scale_down.assert_called_once()
    charm.sentinel_manager.get_primary_ip.assert_not_called()
