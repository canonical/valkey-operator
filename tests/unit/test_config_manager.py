#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for ConfigManager.get_config_properties override block."""

from pathlib import PurePosixPath
from unittest.mock import MagicMock

import pytest

from core.models import PersistenceConfig
from literals import PersistenceMode
from managers.config import ConfigManager
from statuses import CharmStatuses, PersistenceStatuses


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
    # a unit that never applied persistence settings renders the RDB defaults
    state.unit_server.applied_persistence_config = None
    state.config = {}

    workload = MagicMock()
    workload.acl_file.as_posix.return_value = "/var/lib/valkey/users.acl"
    workload.working_dir.as_posix.return_value = "/var/lib/valkey"
    workload.tls_paths.client_cert.as_posix.return_value = "/tls/cert.pem"
    workload.tls_paths.client_key.as_posix.return_value = "/tls/key.pem"
    workload.tls_paths.ca_certs_dir.as_posix.return_value = "/tls/ca"
    # Default RAM is well above the 1 GiB gate; the RAM-gated test overrides
    # this on a per-case basis.
    workload.total_memory_bytes.return_value = 2 * 1024**3
    # no AOF or dump.rdb on the storage
    workload.path_exists.return_value = False

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
def test_min_replicas_to_write_is_static_one_in_file(planned_units):
    """min-replicas-to-write always ships as '1' in valkey.conf.

    The file carries the backup-safe default ('1'); relaxing it to '0' on
    smaller or partially-rolled-out clusters happens only at runtime via
    ClusterManager.reconcile_min_replicas_to_write (reasserted after every
    (re)start, since CONFIG SET does not persist). The rendered file value is
    a constant '1' regardless of topology.
    """
    cm = _make_config_manager(planned_units=planned_units)
    props = cm.get_config_properties(primary_endpoint="10.0.0.5")

    assert props["min-replicas-to-write"] == "1"


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


def test_valkey_logfile_points_to_logs_volume():
    """valkey.conf logs to <log_dir>/valkey.log; dir stays the data volume."""
    cm = _make_config_manager()
    cm.workload.log_dir = PurePosixPath("/var/log/valkey")
    props = cm.get_config_properties(primary_endpoint="10.0.0.5")
    assert props["logfile"] == "/var/log/valkey/valkey.log"
    assert props["dir"] == "/var/lib/valkey"  # unchanged — data volume


def test_sentinel_logfile_points_to_logs_volume(mocker):
    """sentinel.conf gets a top-level logfile in the logs volume."""
    cm = _make_config_manager()
    cm.workload.log_dir = PurePosixPath("/var/log/valkey")
    mocker.patch.object(cm, "_generate_sentinel_configs", return_value={})
    mocker.patch.object(cm, "generate_sentinel_tls_config", return_value={})
    props = cm.get_sentinel_config_properties(primary_endpoint="10.0.0.5")
    assert props["logfile"] == "/var/log/valkey/sentinel.log"


@pytest.mark.parametrize(
    "mode,save,appendonly,del_sync_files",
    [
        (PersistenceMode.RDB, "900 1 300 100 60 10000", "no", "no"),
        (PersistenceMode.AOF, '""', "yes", "no"),
        (PersistenceMode.RDB_AOF, "900 1 300 100 60 10000", "yes", "no"),
        (PersistenceMode.NONE, '""', "no", "yes"),
    ],
)
def test_persistence_keys_follow_the_applied_mode(mode, save, appendonly, del_sync_files):
    """valkey.conf carries DA290's settings for the mode the unit applied."""
    cm = _make_config_manager()
    cm.state.unit_server.applied_persistence_config = PersistenceConfig(mode=mode)

    props = cm.get_config_properties(primary_endpoint="10.0.0.5")

    assert props["save"] == save
    assert props["appendonly"] == appendonly
    assert props["rdb-del-sync-files"] == del_sync_files
    assert props["appendfsync"] == "everysec"
    assert props["aof-timestamp-enabled"] == "yes"


def test_unapplied_unit_renders_rdb_defaults():
    """A unit that never applied the settings renders RDB, even if AOF is stored for the app.

    Only the reconcile may switch AOF on, because a restart into AOF without an AOF on disk
    starts empty.
    """
    cm = _make_config_manager()
    cm.state.cluster.persistence_config = PersistenceConfig(mode=PersistenceMode.AOF)

    props = cm.get_config_properties(primary_endpoint="10.0.0.5")

    assert props["appendonly"] == "no"
    assert props["save"] == "900 1 300 100 60 10000"


def test_explicit_settings_override_the_applied_ones():
    """The reconcile renders the new settings before it records them."""
    cm = _make_config_manager()

    props = cm.get_config_properties(
        primary_endpoint="10.0.0.5", persistence=PersistenceConfig(mode=PersistenceMode.AOF)
    )

    assert props["appendonly"] == "yes"


@pytest.mark.parametrize(
    "has_manifest,has_dump,aof_newer,appendonly",
    [
        (True, True, True, "yes"),
        (True, False, False, "yes"),
        (True, True, False, "no"),
        (False, True, False, "no"),
    ],
)
def test_reused_aof_loads_only_when_newest(has_manifest, has_dump, aof_newer, appendonly):
    """A unit with nothing applied renders the stored AOF mode only for an AOF that is newest."""
    cm = _make_config_manager()
    cm.state.cluster.persistence_config = PersistenceConfig(mode=PersistenceMode.AOF)
    cm.workload.working_dir = PurePosixPath("/var/lib/valkey")
    cm.workload.path_exists.side_effect = lambda path: (
        has_manifest if path.name == "appendonly.aof.manifest" else has_dump
    )
    cm.workload.has_files_newer_than.return_value = aof_newer

    assert cm.get_config_properties(primary_endpoint="10.0.0.5")["appendonly"] == appendonly


def test_invalid_persistence_config_blocks_the_app():
    """An invalid option surfaces a blocked app status."""
    cm = _make_config_manager()
    cm.state.config = {"persistence-mode": "aof"}

    assert PersistenceStatuses.INVALID_CONFIG.value in cm.get_statuses(scope="app")


def test_valid_persistence_config_keeps_the_app_active():
    """A valid config adds no status."""
    cm = _make_config_manager()
    cm.state.config = {"persistence-mode": "AOF"}

    assert cm.get_statuses(scope="app") == [CharmStatuses.ACTIVE_IDLE.value]
