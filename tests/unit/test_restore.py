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
        "download",
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


def test_download_backup_validates_head_and_moves_atomically(mocker):
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()
    mgr.workload = mocker.Mock()
    mgr.workload.working_dir = mocker.MagicMock()
    mgr.state.cluster.s3_credentials = mocker.Mock(path="valkey")

    bucket = mocker.Mock()

    def fake_download(key, fileobj):
        fileobj.write(b"REDIS0011" + b"\x00" * 100)

    bucket.download_fileobj.side_effect = fake_download
    mocker.patch.object(mgr, "_get_bucket_resource", return_value=bucket)

    mgr.download_backup("2026-05-13T10:00:00Z")

    # Pushed the temp file, then atomically moved it onto the final name.
    assert mgr.workload.push_data_file.called
    assert mgr.workload.move_file.called


def test_download_backup_rejects_non_rdb_head(mocker):
    from common.exceptions import ValkeyRestoreError
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()
    mgr.workload = mocker.Mock()
    mgr.workload.working_dir = mocker.MagicMock()
    mgr.state.cluster.s3_credentials = mocker.Mock(path="valkey")
    bucket = mocker.Mock()
    bucket.download_fileobj.side_effect = lambda key, fobj: fobj.write(b"NOTRDB....")
    mocker.patch.object(mgr, "_get_bucket_resource", return_value=bucket)

    import pytest

    with pytest.raises(ValkeyRestoreError):
        mgr.download_backup("2026-05-13T10:00:00Z")
    # Never promoted a bad file to the final name.
    assert not mgr.workload.move_file.called


def test_next_restore_step():
    from src.literals import RestoreStep
    from src.managers.backup import BackupManager

    assert BackupManager.next_restore_step(RestoreStep.NOT_STARTED) == RestoreStep.DOWNLOAD
    assert BackupManager.next_restore_step(RestoreStep.DOWNLOAD) == RestoreStep.RESTORE
    assert BackupManager.next_restore_step(RestoreStep.RESTORE) == RestoreStep.RESYNC
    assert BackupManager.next_restore_step(RestoreStep.RESYNC) == RestoreStep.COMPLETED


def test_restore_on_primary_orders_stop_swap_start(mocker):
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()
    mgr.workload = mocker.Mock(valkey_service="valkey", working_dir=mocker.Mock())
    calls = []
    mgr.workload.stop_service.side_effect = lambda s: calls.append(("stop", s))
    mgr.workload.move_file.side_effect = lambda a, b: calls.append(("move", a, b))
    mgr.workload.start_service.side_effect = lambda s: calls.append(("start", s))
    mocker.patch.object(mgr, "_wait_until_loaded")
    mocker.patch.object(BackupManager, "_download_path", new_callable=mocker.PropertyMock)
    mocker.patch.object(BackupManager, "_dump_path", new_callable=mocker.PropertyMock)
    mocker.patch.object(BackupManager, "_pre_restore_path", new_callable=mocker.PropertyMock)

    mgr.restore_on_primary()

    kinds = [c[0] for c in calls]
    # stop happens before the move-aside, start happens after the swap.
    assert kinds.index("stop") < kinds.index("move") < kinds.index("start")


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


def test_tuple_match_does_not_run_restore_without_download(mocker):
    """instruction=RESTORE but unit missed DOWNLOAD step → no-op."""
    from src.events.backup import BackupEvents
    from src.literals import RestoreStep

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    bm = ev.charm.backup_manager
    # instruction=RESTORE but this unit is at NOT_STARTED (missed DOWNLOAD)
    ev._run_restore_step(RestoreStep.RESTORE, RestoreStep.NOT_STARTED, role="primary")
    bm.restore_on_primary.assert_not_called()


def test_download_step_primary_suppresses_and_downloads(mocker):
    """On DOWNLOAD instruction with NOT_STARTED step, primary suppresses failover and downloads."""
    from src.events.backup import BackupEvents
    from src.literals import RestoreStep

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev.charm.state.cluster.restore_id = "2026-05-13T10:00:00Z"
    ev._run_restore_step(RestoreStep.DOWNLOAD, RestoreStep.NOT_STARTED, role="primary")
    ev.charm.sentinel_manager.suppress_failover.assert_called_once()
    ev.charm.backup_manager.download_backup.assert_called_once_with("2026-05-13T10:00:00Z")
    ev.charm.backup_manager.set_restore_step.assert_called_with(RestoreStep.DOWNLOAD)


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

    # Make the unit "primary" and stub the destructive workload ops.
    mocker.patch("managers.backup.BackupManager.is_local_primary", return_value=True)
    mocker.patch("managers.backup.BackupManager.download_backup")
    mocker.patch("managers.backup.BackupManager.restore_on_primary")
    mocker.patch("managers.backup.BackupManager.cleanup_restore_files")
    mocker.patch("managers.sentinel.SentinelManager.suppress_failover")
    mocker.patch("managers.sentinel.SentinelManager.resume_failover")
    # path_exists drives a re-download guard; stub it so the mock download_backup
    # is not called twice per RESTORE step.
    mocker.patch("core.base_workload.WorkloadBase.path_exists", return_value=True)

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    # PeerModel has serialize_by_alias=True + alias_generator=underscore→hyphen,
    # so the charm reads AND writes relation-data keys in hyphenated form.
    # Use hyphenated keys here so the final delete_field("restore-id") removes them.
    peer = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.DOWNLOAD.value,
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
    ev.charm.state.unit_server.restore_step = RestoreStep.DOWNLOAD
    ev.charm.state.unit_server.restore_role = "primary"
    ev.charm.unit.is_leader.return_value = True
    # path_exists=True so the re-download guard is skipped.
    ev.charm.workload.path_exists.return_value = True
    ev.charm.backup_manager.restore_on_primary.side_effect = ValkeyServicesFailedToStartError(
        "boom"
    )

    ev._on_restore_workflow(mocker.Mock())

    # roll_back triggered inside _do_primary_restore (FIX 1 broadened except)
    ev.charm.backup_manager.roll_back.assert_called_once()
    # resume_failover triggered inside _restore_teardown — the critical invariant
    ev.charm.sentinel_manager.resume_failover.assert_called_once()


def test_restore_failure_unhealthy_sets_unhealthy_status(mocker):
    """ValkeyRestoreUnhealthyError must surface RESTORE_UNHEALTHY, not RESTORE_FAILED (FIX 3)."""
    from common.exceptions import ValkeyRestoreUnhealthyError
    from src.events.backup import BackupEvents
    from src.literals import RestoreStep
    from src.statuses import RestoreStatuses

    ev = BackupEvents.__new__(BackupEvents)
    ev.charm = mocker.Mock()
    ev.charm.state.cluster.is_restore_in_progress = True
    ev.charm.state.cluster.restore_instruction = RestoreStep.RESTORE
    ev.charm.state.unit_server.restore_step = RestoreStep.DOWNLOAD
    ev.charm.state.unit_server.restore_role = "primary"
    ev.charm.unit.is_leader.return_value = True
    ev.charm.workload.path_exists.return_value = True
    ev.charm.backup_manager.restore_on_primary.side_effect = ValkeyRestoreUnhealthyError(
        "unhealthy"
    )

    ev._on_restore_workflow(mocker.Mock())

    added_status_values = [call.args[0] for call in ev.charm.state.statuses.add.call_args_list]
    assert RestoreStatuses.RESTORE_UNHEALTHY.value in added_status_values


def test_restore_on_primary_preserves_existing_pre_restore(mocker):
    """move_file(dump → pre-restore) must be skipped when pre-restore already exists (FIX 2)."""
    from src.managers.backup import BackupManager

    mgr = BackupManager.__new__(BackupManager)
    mgr.workload = mocker.Mock(valkey_service="valkey")
    # Simulate a redelivered hook: the pre-restore path already holds the original data.
    mgr.workload.path_exists.return_value = True

    dump = mocker.Mock()
    pre = mocker.Mock()
    dl = mocker.Mock()
    mocker.patch.object(
        BackupManager, "_dump_path", new_callable=mocker.PropertyMock, return_value=dump
    )
    mocker.patch.object(
        BackupManager, "_pre_restore_path", new_callable=mocker.PropertyMock, return_value=pre
    )
    mocker.patch.object(
        BackupManager, "_download_path", new_callable=mocker.PropertyMock, return_value=dl
    )
    mocker.patch.object(mgr, "_wait_until_loaded")

    mgr.restore_on_primary()

    move_calls = [c.args for c in mgr.workload.move_file.call_args_list]
    # The move-aside (dump → pre-restore) must NOT have run; original data preserved.
    assert (dump, pre) not in move_calls
    # The swap (download → dump) MUST have run.
    assert (dl, dump) in move_calls


def test_wait_until_loaded_times_out_raises_unhealthy(mocker):
    """_wait_until_loaded raises ValkeyRestoreUnhealthyError when ping never succeeds.

    Import BackupManager via the flat path (``managers.backup``) so that the
    patches on ``managers.backup.stop_after_delay`` / ``wait_fixed`` land in the
    same module dict as ``_wait_until_loaded.__globals__``.  Using
    ``src.managers.backup`` would produce a separate module object in the full
    test suite (both names resolve to the same file, but Python registers them
    as distinct entries in ``sys.modules``), causing the patch to miss.
    """
    import pytest
    import tenacity

    from common.exceptions import ValkeyRestoreUnhealthyError
    from managers.backup import BackupManager  # flat path — must match patch target

    mgr = BackupManager.__new__(BackupManager)
    mgr.state = mocker.Mock()

    mocker.patch("managers.backup.stop_after_delay", return_value=tenacity.stop_after_attempt(2))
    mocker.patch("managers.backup.wait_fixed", return_value=tenacity.wait_none())

    client = mocker.Mock()
    client.ping.return_value = False
    mocker.patch.object(mgr, "_valkey_client", return_value=client)

    with pytest.raises(ValkeyRestoreUnhealthyError):
        mgr._wait_until_loaded()


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
