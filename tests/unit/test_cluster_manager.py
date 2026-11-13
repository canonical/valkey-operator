#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for ClusterManager."""

from unittest.mock import MagicMock, patch

import pytest
import tenacity
from charmlibs import pathops

from common.exceptions import ValkeyClusterNotReadyError, ValkeyWorkloadCommandError
from literals import PersistenceMode
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


def _cluster_manager_with_workload() -> ClusterManager:
    """Build a ClusterManager whose workload has a real working_dir for path joins."""
    cm = ClusterManager(state=MagicMock(), workload=MagicMock())
    cm.workload.working_dir = pathops.ContainerPath("/data", container="valkey")
    return cm


# valkey-check-rdb prints the AUX FIELD lines among other, non-metadata, lines.
RDB_CHECK_OUTPUT = (
    "[offset 0] Checking RDB file /data/dump.rdb\n"
    "[offset 26] AUX FIELD redis-ver = '255.255.255'\n"
    "[offset 40] AUX FIELD redis-bits = '64'\n"
    "[offset 52] AUX FIELD ctime = '1794421900'\n"
    "[offset 67] AUX FIELD used-mem = '1197808'\n"
    "[offset 83] AUX FIELD repl-stream-db = '0'\n"
    "[offset 105] AUX FIELD repl-id = '7d8f0a2b3c'\n"
    "[offset 130] AUX FIELD repl-offset = '4242'\n"
    "[offset 145] Checksum OK\n"
    "[info] RDB file was saved with checksum OK\n"
)


def test_get_rdb_metadata_keeps_only_aux_fields():
    """Only the AUX FIELD lines carry the metadata we later parse."""
    cm = _cluster_manager_with_workload()
    cm.workload.check_rdb.return_value = RDB_CHECK_OUTPUT

    with patch("charmlibs.pathops.ContainerPath.exists", return_value=True):
        metadata = cm.get_rdb_metadata()

    assert metadata == [
        "[offset 26] AUX FIELD redis-ver = '255.255.255'",
        "[offset 40] AUX FIELD redis-bits = '64'",
        "[offset 52] AUX FIELD ctime = '1794421900'",
        "[offset 67] AUX FIELD used-mem = '1197808'",
        "[offset 83] AUX FIELD repl-stream-db = '0'",
        "[offset 105] AUX FIELD repl-id = '7d8f0a2b3c'",
        "[offset 130] AUX FIELD repl-offset = '4242'",
    ]


def test_get_rdb_metadata_returns_none_when_the_check_fails():
    """A failed valkey-check-rdb (e.g. no dump.rdb) yields no metadata."""
    cm = _cluster_manager_with_workload()
    cm.workload.check_rdb.side_effect = ValkeyWorkloadCommandError("no such file")

    with patch("charmlibs.pathops.ContainerPath.exists", return_value=True):
        assert cm.get_rdb_metadata() is None


AOF_MANIFEST = (
    "file appendonly.aof.1.base.rdb seq 1 type b\n"
    "file appendonly.aof.1.incr.aof seq 1 type i\n"
    "file appendonly.aof.2.incr.aof seq 2 type i\n"
)


def test_get_latest_aof_returns_the_last_incr_file():
    """The newest incremental file in the manifest is the one to read a timestamp from."""
    cm = _cluster_manager_with_workload()
    cm.workload.read_file.return_value = AOF_MANIFEST

    with patch("charmlibs.pathops.ContainerPath.exists", return_value=True):
        assert cm.get_latest_aof() == "/data/appendonlydir/appendonly.aof.2.incr.aof"


def test_get_latest_aof_returns_none_without_an_incr_file():
    """A manifest with only a base file has no incremental timestamps to read."""
    cm = _cluster_manager_with_workload()
    cm.workload.read_file.return_value = "file appendonly.aof.1.base.rdb seq 1 type b\n"

    with patch("charmlibs.pathops.ContainerPath.exists", return_value=True):
        assert cm.get_latest_aof() is None


def test_get_latest_aof_returns_none_when_the_manifest_is_unreadable():
    """No manifest (fresh unit or no AOF) means there is no AOF to inspect."""
    cm = _cluster_manager_with_workload()
    cm.workload.read_file.side_effect = ValkeyWorkloadCommandError("no manifest")

    with patch("charmlibs.pathops.ContainerPath.exists", return_value=True):
        assert cm.get_latest_aof() is None


def test_get_latest_timestamp_from_aof_extracts_the_timestamp():
    """The reverse-grep returns the raw '#TS:...' line, we keep only the epoch value."""
    cm = _cluster_manager_with_workload()
    cm.workload.exec.return_value = ("#TS:1794421964\r\n", "")

    assert cm.get_latest_timestamp_from_aof("/data/appendonlydir/appendonly.aof.2.incr.aof") == (
        "1794421964"
    )


def test_get_latest_timestamp_from_aof_ignores_invalid_values():
    """Test the best-effort timestamp search validation."""
    cm = _cluster_manager_with_workload()
    cm.workload.exec.return_value = ("#TS:InvalidValue\r\n", "")

    assert (
        cm.get_latest_timestamp_from_aof("/data/appendonlydir/appendonly.aof.2.incr.aof") is None
    )


def test_get_latest_timestamp_from_aof_returns_none_on_error():
    """A failed search (e.g. empty AOF) yields no timestamp rather than raising."""
    cm = _cluster_manager_with_workload()
    cm.workload.exec.side_effect = ValkeyWorkloadCommandError("grep failed")

    assert (
        cm.get_latest_timestamp_from_aof("/data/appendonlydir/appendonly.aof.2.incr.aof") is None
    )


def test_read_local_data_offset_parses_rdb_metadata():
    """RDB mode stores the replication id, offset and ctime read from the RDB file."""
    cm = _cluster_manager_with_workload()
    cm.state.unit_server.applied_persistence_config.mode = PersistenceMode.RDB
    cm.get_rdb_metadata = MagicMock(
        return_value=[
            "AUX FIELD ctime = '1794421900'",
            "AUX FIELD repl-id = '7d8f0a2b3c'",
            "AUX FIELD repl-offset = '4242'",
        ]
    )

    cm.read_local_data_offset()

    cm.state.unit_server.store_local_data_offset.assert_called_once_with(
        "7d8f0a2b3c", "4242", "1794421900"
    )


def test_read_local_data_offset_uses_aof_timestamp_in_aof_mode():
    """AOF mode has no replication id/offset, only the timestamp read from the AOF."""
    cm = _cluster_manager_with_workload()
    cm.state.unit_server.applied_persistence_config.mode = PersistenceMode.AOF
    cm.get_latest_aof = MagicMock(return_value="/data/appendonlydir/appendonly.aof.2.incr.aof")
    cm.get_latest_timestamp_from_aof = MagicMock(return_value="1794421964")

    cm.read_local_data_offset()

    cm.state.unit_server.store_local_data_offset.assert_called_once_with("0", "0", "1794421964")


def test_read_local_data_offset_aof_timestamp_overrides_rdb_ctime():
    """RDB+AOF reads only the AOF timestamp; the RDB replication id/offset are left unset.

    The RDB block runs for pure RDB mode only, so a RDB+AOF unit reports all
    zeros for the replication id/offset and relies on the AOF timestamp.
    """
    cm = _cluster_manager_with_workload()
    cm.state.unit_server.applied_persistence_config.mode = PersistenceMode.RDB_AOF
    cm.get_rdb_metadata = MagicMock(
        return_value=[
            "AUX FIELD ctime = '1794421900'",
            "AUX FIELD repl-id = '7d8f0a2b3c'",
            "AUX FIELD repl-offset = '4242'",
        ]
    )
    cm.get_latest_aof = MagicMock(return_value="/data/appendonlydir/appendonly.aof.2.incr.aof")
    cm.get_latest_timestamp_from_aof = MagicMock(return_value="1794421964")

    cm.read_local_data_offset()

    cm.get_rdb_metadata.assert_not_called()
    cm.state.unit_server.store_local_data_offset.assert_called_once_with("0", "0", "1794421964")


def test_read_local_data_offset_stores_zeros_without_any_data():
    """No RDB and no AOF on disk stores the 'no data found' sentinel of all zeros."""
    cm = _cluster_manager_with_workload()
    cm.state.cluster.persistence_config.mode = PersistenceMode.RDB
    cm.get_rdb_metadata = MagicMock(return_value=None)

    cm.read_local_data_offset()

    cm.state.unit_server.store_local_data_offset.assert_called_once_with("0", "0", "0")
