#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the S3 restore feature."""

from src.core.models import PeerAppModel, PeerUnitModel
from src.literals import RestoreStep


def test_restore_step_order_and_values():
    assert RestoreStep.NOT_STARTED.value == ""
    assert [s.value for s in RestoreStep] == [
        "",
        "restore",
        "resync",
        "completed",
    ]


def test_new_model_fields_default_falsy():
    app = PeerAppModel()
    assert app.restore_id == ""
    assert app.restore_instruction == ""
    assert app.restore_participants == ""
    unit = PeerUnitModel()
    assert unit.restore_step == ""
    assert unit.restore_role == ""


def test_model_tolerates_missing_keys():
    # Old-revision databag (no restore_* keys) must still parse.
    app = PeerAppModel.model_validate({"start_member": "valkey/0"})
    assert app.restore_id == ""
    unit = PeerUnitModel.model_validate({"hostname": "h"})
    assert unit.restore_step == ""


def test_valkey_server_restore_step_maps_enum(mocker):
    from src.core.models import ValkeyServer

    srv = mocker.Mock()
    srv.model = mocker.Mock(restore_step="restore")
    assert ValkeyServer.restore_step.fget(srv) == RestoreStep.RESTORE
    srv.model = None
    assert ValkeyServer.restore_step.fget(srv) == RestoreStep.NOT_STARTED


def test_valkey_cluster_is_restore_in_progress(mocker):
    from src.core.models import ValkeyCluster

    cl = mocker.Mock()
    cl.model = mocker.Mock(restore_id="2026-05-13T10:00:00Z")
    assert ValkeyCluster.is_restore_in_progress.fget(cl) is True
    cl.model = mocker.Mock(restore_id="")
    assert ValkeyCluster.is_restore_in_progress.fget(cl) is False


def test_barrier_fails_closed_on_departed_participant(mocker):
    from src.core.cluster_state import ClusterState
    from src.literals import RestoreStep

    def srv(name, step):
        return mocker.Mock(unit_name=name, restore_step=step)

    cs = mocker.Mock(spec=ClusterState)
    # Only valkey/0 and valkey/1 are live; valkey/2 departed.
    cs.servers = {
        srv("valkey/0", RestoreStep.RESTORE),
        srv("valkey/1", RestoreStep.RESTORE),
    }
    cs.cluster = mocker.Mock(
        restore_instruction=RestoreStep.RESTORE,
        restore_participants=["valkey/0", "valkey/1", "valkey/2"],
    )
    # Call the real implementation against the mock.
    assert ClusterState.can_restore_workflow_proceed.fget(cs) is False


def test_barrier_passes_when_all_participants_reached(mocker):
    from src.core.cluster_state import ClusterState
    from src.literals import RestoreStep

    def srv(name, step):
        return mocker.Mock(unit_name=name, restore_step=step)

    cs = mocker.Mock(spec=ClusterState)
    cs.servers = {srv("valkey/0", RestoreStep.RESTORE), srv("valkey/1", RestoreStep.RESTORE)}
    cs.cluster = mocker.Mock(
        restore_instruction=RestoreStep.RESTORE,
        restore_participants=["valkey/0", "valkey/1"],
    )
    assert ClusterState.can_restore_workflow_proceed.fget(cs) is True


def test_is_backup_in_progress_any_checks_all_servers(mocker):
    from src.core.cluster_state import ClusterState

    cs = mocker.Mock(spec=ClusterState)
    cs.servers = {
        mocker.Mock(is_backup_in_progress=False),
        mocker.Mock(is_backup_in_progress=True),  # a backup on a *different* unit
    }
    assert ClusterState.is_backup_in_progress_any.fget(cs) is True
    cs.servers = {mocker.Mock(is_backup_in_progress=False)}
    assert ClusterState.is_backup_in_progress_any.fget(cs) is False


def test_restore_statuses_present():
    from src.statuses import RestoreStatuses

    assert RestoreStatuses.RESTORE_IN_PROGRESS.value.status == "maintenance"
    assert RestoreStatuses.RESTORE_FAILED.value.status == "blocked"
    assert RestoreStatuses.RESTORE_UNHEALTHY.value.status == "blocked"
    assert RestoreStatuses.RESTORE_FAILED.value.running == "async"


def test_workload_has_new_primitives():
    from src.core.base_workload import WorkloadBase

    for name in ("stop_service", "start_service", "push_data_file", "move_file"):
        assert getattr(WorkloadBase, name).__isabstractmethod__ is True


def test_vm_stop_service_stops_only_that_service(mocker):
    from src.workload_vm import ValkeyVmWorkload

    wl = ValkeyVmWorkload.__new__(ValkeyVmWorkload)
    wl.valkey = mocker.Mock()
    wl.valkey_service = "server"
    # Pretend the service is stopped after the call.
    wl.valkey.services = {"server": {"active": False}}
    wl.stop_service("server")
    wl.valkey.stop.assert_called_once_with(services=["server"])


def test_k8s_move_file_uses_container_exec(mocker):
    from src.workload_k8s import ValkeyK8sWorkload

    wl = ValkeyK8sWorkload.__new__(ValkeyK8sWorkload)
    wl.container = mocker.Mock()
    src = mocker.Mock(as_posix=lambda: "/var/lib/valkey/dump.rdb")
    dest = mocker.Mock(as_posix=lambda: "/var/lib/valkey/dump.rdb.pre-restore")
    wl.move_file(src, dest)
    wl.container.exec.assert_called_once()
    args = wl.container.exec.call_args.kwargs["command"]
    assert args == ["mv", "/var/lib/valkey/dump.rdb", "/var/lib/valkey/dump.rdb.pre-restore"]


def test_suppress_and_resume_failover_iterate_all_sentinels(mocker):
    from src.literals import (
        PRIMARY_NAME,
        SENTINEL_DOWN_AFTER_MS,
        SENTINEL_DOWN_AFTER_SUPPRESSED_MS,
    )
    from src.managers.sentinel import SentinelManager

    mgr = SentinelManager.__new__(SentinelManager)
    client = mocker.Mock()
    mocker.patch.object(mgr, "_get_sentinel_client", return_value=client)
    mocker.patch.object(mgr, "all_sentinel_endpoints", return_value=["10.0.0.1", "10.0.0.2"])

    mgr.suppress_failover()
    client.set.assert_any_call(
        "10.0.0.1", PRIMARY_NAME, "down-after-milliseconds", str(SENTINEL_DOWN_AFTER_SUPPRESSED_MS)
    )
    client.set.assert_any_call(
        "10.0.0.2", PRIMARY_NAME, "down-after-milliseconds", str(SENTINEL_DOWN_AFTER_SUPPRESSED_MS)
    )

    client.reset_mock()
    mgr.resume_failover()
    client.set.assert_any_call(
        "10.0.0.1", PRIMARY_NAME, "down-after-milliseconds", str(SENTINEL_DOWN_AFTER_MS)
    )
    client.reset.assert_any_call(hostname="10.0.0.2")


def test_sentinel_is_failover_in_progress_reads_flags(mocker):
    """Manager helper reports failover from the primary flags with no retry/blocking."""
    from src.managers.sentinel import SentinelManager

    mgr = SentinelManager.__new__(SentinelManager)
    mgr.state = mocker.Mock(endpoint="10.0.0.1")
    client = mocker.Mock()
    mocker.patch.object(mgr, "_get_sentinel_client", return_value=client)

    client.primary.return_value = {"flags": "master,failover_in_progress"}
    assert mgr.is_failover_in_progress() is True

    client.primary.return_value = {"flags": "master"}
    assert mgr.is_failover_in_progress() is False


def test_download_backup_streams_body_to_data_partition_and_moves_atomically(mocker):
    """The full RDB streams straight to the data partition; no whole-object charm buffer.

    The magic header is validated up front by verify_backup_is_rdb (a tiny ranged
    GET), so download_backup itself just streams the S3 body onto disk.
    """
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()
    mgr.workload = mocker.Mock()
    mgr.workload.working_dir = mocker.MagicMock()  # download lands on the data partition
    mgr.state.cluster.s3_credentials = mocker.Mock(path="valkey")

    bucket = mocker.Mock()
    body = mocker.Mock()  # the S3 StreamingBody
    bucket.Object.return_value.get.return_value = {"Body": body}
    mocker.patch.object(mgr, "_get_bucket_resource", return_value=bucket)

    mgr.download_backup("2026-05-13T10:00:00Z")

    # Fetched the object by key and pushed the streaming body straight through --
    # the pushed source is the S3 body itself, not a charm-local temp file.
    bucket.Object.assert_called_once_with("valkey/2026-05-13T10:00:00Z")
    assert mgr.workload.push_data_file.call_args.args[0] is body
    # Then atomically promoted the .part file onto the final name.
    assert mgr.workload.move_file.called


def test_verify_backup_is_rdb_accepts_valid_head(mocker):
    """Pre-stop check reads only the head via a ranged GET and passes a real RDB magic."""
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()
    mgr.state.cluster.s3_credentials = mocker.Mock(path="valkey")
    bucket = mocker.Mock()
    bucket.Object.return_value.get.return_value = {"Body": mocker.Mock(read=lambda: b"REDIS0011")}
    mocker.patch.object(mgr, "_get_bucket_resource", return_value=bucket)

    mgr.verify_backup_is_rdb("2026-05-13T10:00:00Z")  # no raise

    # ranged GET of just the head, not the whole object
    bucket.Object.assert_called_once_with("valkey/2026-05-13T10:00:00Z")
    bucket.Object.return_value.get.assert_called_once_with(Range="bytes=0-15")


def test_verify_backup_is_rdb_rejects_bad_head(mocker):
    """A non-RDB object is rejected by the pre-stop check (before valkey is touched)."""
    import pytest

    from common.exceptions import ValkeyRestoreError
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()
    mgr.state.cluster.s3_credentials = mocker.Mock(path="valkey")
    bucket = mocker.Mock()
    bucket.Object.return_value.get.return_value = {
        "Body": mocker.Mock(read=lambda: b"NOT-AN-RDB..")
    }
    mocker.patch.object(mgr, "_get_bucket_resource", return_value=bucket)

    with pytest.raises(ValkeyRestoreError):
        mgr.verify_backup_is_rdb("2026-05-13T10:00:00Z")


def test_do_primary_restore_validates_before_touching_valkey(mocker):
    """A bad backup fails the pre-stop check, so the primary is never stopped or rolled back."""
    import pytest

    from common.exceptions import ValkeyRestoreError
    from src.events.backup import BackupEvents

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    bm = ev.charm.backup_manager
    bm.verify_backup_is_rdb.side_effect = ValkeyRestoreError("not an RDB")

    with pytest.raises(ValkeyRestoreError):
        ev._do_primary_restore()

    bm.restore_on_primary.assert_not_called()
    bm.roll_back.assert_not_called()


def test_restore_files_on_correct_partitions(mocker):
    """Only the rollback copy is on archive; the dump and its download temp stay on data."""
    import pathlib

    from src.literals import PRE_RESTORE_SUFFIX
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.workload = mocker.Mock()
    mgr.workload.working_dir = pathlib.PurePosixPath("/data")
    mgr.workload.archive_dir = pathlib.PurePosixPath("/archive")

    # New dump is downloaded straight onto the data partition (single write, no
    # cross-device install); only the pre-restore rollback copy goes to archive.
    assert str(mgr._dump_path) == "/data/dump.rdb"
    assert str(mgr._dump_tmp_path) == "/data/dump.rdb.part"
    assert str(mgr._pre_restore_path) == f"/archive/dump.rdb{PRE_RESTORE_SUFFIX}"


def test_vm_move_file_uses_shutil_move_for_cross_device(mocker):
    """VM move_file uses shutil.move so cross-partition (data<->archive) moves work."""
    from src.workload_vm import ValkeyVmWorkload

    wl = ValkeyVmWorkload.__new__(ValkeyVmWorkload)
    move = mocker.patch("workload_vm.shutil.move")
    src = mocker.Mock(as_posix=lambda: "/data/dump.rdb")
    dest = mocker.Mock(as_posix=lambda: "/archive/dump.rdb.pre-restore")
    wl.move_file(src, dest)
    move.assert_called_once_with("/data/dump.rdb", "/archive/dump.rdb.pre-restore")


def test_next_restore_step():
    from src.literals import RestoreStep
    from src.managers.backup import BackupManager

    assert BackupManager.next_restore_step(RestoreStep.NOT_STARTED) == RestoreStep.RESTORE
    assert BackupManager.next_restore_step(RestoreStep.RESTORE) == RestoreStep.RESYNC
    assert BackupManager.next_restore_step(RestoreStep.RESYNC) == RestoreStep.COMPLETED


def test_restore_on_primary_orders_stop_backup_download_start(mocker):
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()
    mgr.workload = mocker.Mock(valkey_service="valkey", working_dir=mocker.Mock())
    mgr.workload.path_exists.return_value = False  # no existing pre-restore -> move-aside runs
    calls = []
    mgr.workload.stop_service.side_effect = lambda s: calls.append("stop")
    mgr.workload.move_file.side_effect = lambda a, b: calls.append("move")  # dump -> pre-restore
    mgr.workload.start_service.side_effect = lambda s: calls.append("start")
    mocker.patch.object(mgr, "download_backup", side_effect=lambda bid: calls.append("download"))
    mocker.patch.object(BackupManager, "_dump_path", new_callable=mocker.PropertyMock)
    mocker.patch.object(BackupManager, "_pre_restore_path", new_callable=mocker.PropertyMock)

    mgr.restore_on_primary()

    # back up the current dump first, download the new one onto the freed data
    # partition, then start -- the download must land after the move-aside. The
    # health wait is now the caller's job (cluster_manager.wait_until_loaded).
    assert calls == ["stop", "move", "download", "start"]


def test_roll_back_stops_service_before_swap(mocker):
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.workload = mocker.Mock(valkey_service="valkey")
    calls = []
    mgr.workload.stop_service.side_effect = lambda s: calls.append("stop")
    mgr.workload.move_file.side_effect = lambda a, b: calls.append("move")
    mgr.workload.start_service.side_effect = lambda s: calls.append("start")
    mocker.patch.object(BackupManager, "_dump_path", new_callable=mocker.PropertyMock)
    mocker.patch.object(BackupManager, "_pre_restore_path", new_callable=mocker.PropertyMock)
    mgr.workload.path_exists.return_value = True

    mgr.roll_back()
    assert calls == ["stop", "move", "start"]


def test_get_statuses_reports_restore_in_progress(mocker):
    from src.managers.backup import BackupManager
    from src.statuses import RestoreStatuses

    mgr = BackupManager.__new__(BackupManager)
    mgr.name = "backup"
    mgr.state = mocker.Mock()
    mgr.state.statuses.get.return_value.root = []
    mgr.state.cluster.is_restore_in_progress = True
    mgr.state.s3_relation = None
    assert RestoreStatuses.RESTORE_IN_PROGRESS.value in mgr.get_statuses(scope="app")


def test_restore_blocking_reason_rejects_non_leader(mocker):
    from src.events.backup import BackupEvents

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev.charm.unit.is_leader.return_value = False
    assert "leader" in ev._restore_blocking_reason().lower()


def test_blocking_reason_blocks_backup_during_restore(mocker):
    from src.events.backup import BackupEvents

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev.charm.state.s3_relation = True
    ev.charm.state.cluster.s3_credentials = True
    ev.charm.workload.alive.return_value = True
    ev.charm.state.unit_server.is_backup_in_progress = False
    ev.charm.state.cluster.is_restore_in_progress = True
    # create-backup (check_running_operations=True) is blocked...
    assert ev._blocking_reason(check_running_operations=True) is not None
    # ...but list-backups (False) is NOT blocked by a restore.
    ev.charm.state.cluster.is_restore_in_progress = True
    assert ev._blocking_reason(check_running_operations=False) is None


# ── Task-10 tests: _on_restore_workflow state machine ───────────────────────


def test_tuple_match_skips_step_when_prior_not_reached(mocker):
    """A later instruction is a no-op unless this unit reached the exact prior step.

    instruction=RESYNC but the unit never recorded RESTORE → it must not run the
    resync work nor advance its own step, so a unit that missed the fused
    RESTORE can never act out of order.
    """
    from src.events.backup import BackupEvents
    from src.literals import RestoreStep

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    bm = ev.charm.backup_manager
    ev._run_restore_step(RestoreStep.RESYNC, RestoreStep.NOT_STARTED, role="replica")
    ev.charm.cluster_manager.wait_until_resynced.assert_not_called()
    ev.charm.sentinel_manager.resume_failover.assert_not_called()
    bm.set_restore_step.assert_not_called()


def test_restore_step_primary_suppresses_and_restores(mocker):
    """On RESTORE from NOT_STARTED, the primary suppresses failover then restores.

    The download now happens inside restore_on_primary (after stop + move-aside),
    so the step just drives suppress + restore_on_primary.
    """
    from src.events.backup import BackupEvents
    from src.literals import RestoreStep

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev.charm.cluster_manager.is_primary.return_value = True
    ev._run_restore_step(RestoreStep.RESTORE, RestoreStep.NOT_STARTED, role="")
    ev.charm.sentinel_manager.suppress_failover.assert_called_once()
    ev.charm.backup_manager.restore_on_primary.assert_called_once()
    ev.charm.backup_manager.set_restore_step.assert_called_with(RestoreStep.RESTORE)


def test_restore_step_replica_records_role_without_restoring(mocker):
    """A replica on the fused RESTORE step records its role/step but never touches the dataset."""
    from src.events.backup import BackupEvents
    from src.literals import RestoreStep

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev.charm.cluster_manager.is_primary.return_value = False
    ev._run_restore_step(RestoreStep.RESTORE, RestoreStep.NOT_STARTED, role="")
    ev.charm.sentinel_manager.suppress_failover.assert_not_called()
    ev.charm.backup_manager.download_backup.assert_not_called()
    ev.charm.backup_manager.restore_on_primary.assert_not_called()
    ev.charm.state.unit_server.update.assert_called_once_with({"restore_role": "replica"})
    ev.charm.backup_manager.set_restore_step.assert_called_with(RestoreStep.RESTORE)


def test_teardown_resumes_suppression_and_marks_failed(mocker):
    """_restore_teardown always calls resume_failover regardless of who caused the failure."""
    from src.events.backup import BackupEvents

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev._restore_teardown()
    ev.charm.sentinel_manager.resume_failover.assert_called_once()


def test_single_unit_restore_reaches_completed(mocker, cloud_spec):
    """A single-unit cluster (leader = primary) runs the full restore workflow to COMPLETED."""
    import pytest

    pytest.importorskip("ops.testing")

    from ops import testing

    from src.charm import ValkeyCharm
    from src.literals import PEER_RELATION, S3_RELATION_NAME, STATUS_PEERS_RELATION, RestoreStep

    # Make the unit "primary" and stub the destructive workload ops and the
    # post-restore health/resync waits (now owned by the cluster manager).
    mocker.patch("managers.cluster.ClusterManager.is_primary", return_value=True)
    mocker.patch("managers.cluster.ClusterManager.wait_until_loaded")
    mocker.patch("managers.cluster.ClusterManager.wait_until_resynced")
    mocker.patch("managers.backup.BackupManager.verify_backup_is_rdb")
    mocker.patch("managers.backup.BackupManager.download_backup")
    mocker.patch("managers.backup.BackupManager.restore_on_primary")
    mocker.patch("managers.backup.BackupManager.cleanup_restore_files")
    mocker.patch("managers.sentinel.SentinelManager.suppress_failover")
    mocker.patch("managers.sentinel.SentinelManager.resume_failover")

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    # PeerModel serializes with hyphenated aliases, so use hyphenated keys here
    # (the final delete_field("restore-id") must match).
    peer = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.RESTORE.value,
            "restore-participants": "valkey/0",
        },
        local_unit_data={"start-state": "started"},
    )
    status_peer = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    s3_rel = testing.Relation(
        id=3,
        endpoint=S3_RELATION_NAME,
        interface="s3",
        remote_app_name="s3-integrator",
    )
    state_in = testing.State(
        model=testing.Model(name="m", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={peer, status_peer, s3_rel},
        containers={testing.Container(name="valkey", can_connect=True)},
    )

    # Each update_status advances two steps (TLS-emitted relation_changed + direct
    # update_status handler both fire _on_restore_workflow). Drive to completion.
    state = state_in
    for _ in range(8):
        state = ctx.run(ctx.on.update_status(), state)
        peer_out = next(r for r in state.relations if r.endpoint == PEER_RELATION)
        if not peer_out.local_app_data.get("restore-id"):
            break
    peer_out = next(r for r in state.relations if r.endpoint == PEER_RELATION)
    assert peer_out.local_app_data.get("restore-id", "") == ""


# ── Task-11 tests: restore-awareness guards ──────────────────────────────────


def test_storage_detaching_refuses_during_restore(mocker):
    from src.events.base_events import BaseEvents

    ev = BaseEvents.__new__(BaseEvents)
    ev.charm = mocker.Mock()
    # Not already scaled down (the is_being_removed early-return runs first).
    ev.charm.state.unit_server.is_being_removed = False
    ev.charm.state.unit_server.is_backup_in_progress = False
    ev.charm.state.cluster.is_restore_in_progress = True
    import pytest

    with pytest.raises(Exception):  # ValkeyBackupInProgressError or a restore-specific error
        ev._on_storage_detaching(mocker.Mock())


def test_restart_workload_defers_during_restore(mocker):
    from src.charm import ValkeyCharm

    charm = ValkeyCharm.__new__(ValkeyCharm)
    charm.state = mocker.Mock()
    charm.state.unit_server.is_backup_in_progress = False
    charm.state.cluster.is_restore_in_progress = True
    event = mocker.Mock()
    ValkeyCharm._on_restart_workload(charm, event)
    event.defer.assert_called_once()


def test_external_clients_prc_skips_during_restore(mocker):
    from src.events.external_clients import ExternalClientsEvents

    ev = ExternalClientsEvents.__new__(ExternalClientsEvents)
    ev.charm = mocker.Mock()
    ev.charm.state.unit_server.is_started = True
    ev.charm.state.cluster.is_restore_in_progress = True
    ev._on_peer_relation_changed(mocker.Mock())
    ev.charm.sentinel_manager.reconcile_k8s_services.assert_not_called()


# ── Final-review fixes (FIX 1–5) ────────────────────────────────────────────


def test_restore_failure_service_error_resumes_suppression(mocker):
    """ValkeyServicesFailedToStartError from restore_on_primary must reach teardown.

    This is the critical regression test for FIX 1: service-control errors are
    standalone Exception subclasses not in the original narrow except tuple, so
    without the broad catch they escape resume_failover() entirely.
    """
    from common.exceptions import ValkeyServicesFailedToStartError
    from src.events.backup import BackupEvents
    from src.literals import RestoreStep

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev.charm.state.cluster.is_restore_in_progress = True
    ev.charm.state.cluster.restore_instruction = RestoreStep.RESTORE
    ev.charm.state.unit_server.restore_step = RestoreStep.NOT_STARTED
    ev.charm.state.unit_server.restore_role = "primary"
    ev.charm.unit.is_leader.return_value = True
    ev.charm.cluster_manager.is_primary.return_value = True
    ev.charm.backup_manager.restore_on_primary.side_effect = ValkeyServicesFailedToStartError(
        "boom"
    )

    ev._on_restore_workflow(mocker.Mock())

    # roll_back triggered inside _do_primary_restore (FIX 1 broadened except)
    ev.charm.backup_manager.roll_back.assert_called_once()
    # resume_failover triggered inside _restore_teardown — the critical invariant
    ev.charm.sentinel_manager.resume_failover.assert_called_once()


def test_restore_failure_unhealthy_sets_unhealthy_status(mocker):
    """A cluster-not-ready failure must surface RESTORE_UNHEALTHY, not RESTORE_FAILED.

    The unhealthy signal now comes from cluster_manager.wait_until_loaded raising
    ValkeyClusterNotReadyError after the RDB swap, which teardown maps to
    RESTORE_UNHEALTHY.
    """
    from common.exceptions import ValkeyClusterNotReadyError
    from src.events.backup import BackupEvents
    from src.literals import RestoreStep
    from src.statuses import RestoreStatuses

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev.charm.state.cluster.is_restore_in_progress = True
    ev.charm.state.cluster.restore_instruction = RestoreStep.RESTORE
    ev.charm.state.unit_server.restore_step = RestoreStep.NOT_STARTED
    ev.charm.state.unit_server.restore_role = "primary"
    ev.charm.unit.is_leader.return_value = True
    ev.charm.cluster_manager.is_primary.return_value = True
    ev.charm.cluster_manager.wait_until_loaded.side_effect = ValkeyClusterNotReadyError(
        "unhealthy"
    )

    ev._on_restore_workflow(mocker.Mock())

    # roll_back runs (the RDB swap succeeded but the server never came up healthy).
    ev.charm.backup_manager.roll_back.assert_called_once()
    added_status_values = [call.args[0] for call in ev.charm.state.statuses.add.call_args_list]
    assert RestoreStatuses.RESTORE_UNHEALTHY.value in added_status_values


def test_restore_on_primary_preserves_existing_pre_restore(mocker):
    """move-aside (dump → pre-restore) must be skipped when pre-restore already exists (FIX 2)."""
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()
    mgr.workload = mocker.Mock(valkey_service="valkey")
    # Simulate a redelivered hook: the pre-restore path already holds the original data.
    mgr.workload.path_exists.return_value = True

    dump = mocker.Mock()
    pre = mocker.Mock()
    mocker.patch.object(
        BackupManager, "_dump_path", new_callable=mocker.PropertyMock, return_value=dump
    )
    mocker.patch.object(
        BackupManager, "_pre_restore_path", new_callable=mocker.PropertyMock, return_value=pre
    )
    mocker.patch.object(mgr, "download_backup")

    mgr.restore_on_primary()

    move_calls = [c.args for c in mgr.workload.move_file.call_args_list]
    # The move-aside (dump → pre-restore) must NOT have run; original data preserved.
    assert (dump, pre) not in move_calls
    # The restore RDB is still downloaded onto the (preserved) data partition.
    mgr.download_backup.assert_called_once()


def test_on_restore_action_rejects_unknown_backup_id(mocker):
    """restore-backup action must fail when backup-id is not in the bucket."""
    from src.events.backup import BackupEvents

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    mocker.patch.object(ev, "_restore_blocking_reason", return_value=None)
    ev.charm.backup_manager.list_backups.return_value = ["2026-05-13T10:00:00Z"]

    event = mocker.Mock()
    event.params = {"backup-id": "nope"}

    ev._on_restore_action(event)

    event.fail.assert_called_once()
    assert "not found" in event.fail.call_args.args[0].lower()
    ev.charm.state.cluster.update.assert_not_called()
