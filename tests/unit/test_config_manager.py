#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for ConfigManager.get_config_properties override block."""

from unittest.mock import MagicMock

import pytest

from managers.config import ConfigManager


def _make_config_manager(planned_units: int = 3) -> ConfigManager:
    """Build a ConfigManager wired to MagicMock state and workload.

    Just enough wiring to let get_config_properties run end-to-end without
    needing a full ops.testing.Context.
    """
    state = MagicMock()
    # The early-return guard checks truthiness of both .model attributes;
    # MagicMock attributes are truthy by default, so this passes the guard.
    state.endpoint = "10.0.0.5"
    state.cluster.internal_users_credentials = {"valkey-replica": "pw"}
    # No TLS for these tests — tls_client_state must not be in the TLS-enabled set.
    state.unit_server.tls_client_state = None
    state.unit_server.model.client_cert_ready = False
    state.charm.app.planned_units.return_value = planned_units

    workload = MagicMock()
    workload.acl_file.as_posix.return_value = "/var/lib/valkey/users.acl"
    workload.working_dir.as_posix.return_value = "/var/lib/valkey"
    workload.tls_paths.client_cert.as_posix.return_value = "/tls/cert.pem"
    workload.tls_paths.client_key.as_posix.return_value = "/tls/key.pem"
    workload.tls_paths.ca_certs_dir.as_posix.return_value = "/tls/ca"
    # Default RAM is well above the 1 GiB gate; the RAM-gated test overrides
    # this on a per-case basis.
    workload.total_memory_bytes.return_value = 2 * 1024**3

    return ConfigManager(state=state, workload=workload)


def test_static_overrides_land_in_rendered_dict():
    """Verify the override block sets the static backup-safety directives."""
    cm = _make_config_manager()
    props = cm.get_config_properties(primary_endpoint="10.0.0.5")

    assert props["repl-diskless-load"] == "on-empty-db"
    assert props["save"] == "900 1 300 100 60 10000"
    assert props["maxmemory-policy"] == "noeviction"
    assert props["min-replicas-max-lag"] == "10"
    assert props["repl-backlog-size"] == "256mb"


@pytest.mark.parametrize("planned_units", [0, 1, 2, 3, 5])
def test_min_replicas_to_write_is_static_zero_in_file(planned_units):
    """min-replicas-to-write always ships as '0' in valkey.conf.

    Requiring an in-sync replica is enabled only at runtime, and only on >= 3
    unit clusters, via ClusterManager.reconcile_min_replicas_to_write. The
    rendered file value is a constant '0' so a fresh 1- or 2-unit primary is
    never write-frozen at startup.
    """
    cm = _make_config_manager(planned_units=planned_units)
    props = cm.get_config_properties(primary_endpoint="10.0.0.5")

    assert props["min-replicas-to-write"] == "0"


@pytest.mark.parametrize(
    "ram_bytes,expected_present,expected_value",
    [
        (512 * 1024**2, False, None),  # 0.5 GiB -> not set
        (1 * 1024**3, False, None),  # exactly 1 GiB -> not set (strict >)
        (2 * 1024**3, True, "256mb"),  # 2 GiB -> set
        (32 * 1024**3, True, "256mb"),  # 32 GiB -> set
    ],
)
def test_repl_backlog_size_is_ram_gated(ram_bytes, expected_present, expected_value):
    """repl-backlog-size is RAM-gated at the strict 1 GiB threshold.

    Set to 256mb only when total RAM is strictly greater than 1 GiB;
    smaller hosts fall back to the server default (10mb).
    """
    cm = _make_config_manager()
    cm.workload.total_memory_bytes.return_value = ram_bytes

    props = cm.get_config_properties(primary_endpoint="10.0.0.5")

    if expected_present:
        assert props["repl-backlog-size"] == expected_value
    else:
        assert "repl-backlog-size" not in props
