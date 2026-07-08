#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the S3 restore feature.

Two layers, on purpose (PR #79 review, reneradoi):

* The restore **state machine** and the **restore action** are exercised through
  real Juju events (``ctx.on.action`` / ``ctx.on.update_status``) via
  ``ops.testing``, so the peer-relation data-interface integration is covered
  end-to-end and a later refactor of the private handlers won't silently gut the
  tests. See the "event-driven" section.
* Managers, workloads, models, and pure helpers are unit-tested directly below
  the event layer, where that granularity belongs.
"""

import pytest

from src.core.models import PeerAppModel, PeerUnitModel
from src.literals import RestoreStep

# ── models / enums (pure) ────────────────────────────────────────────────────


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


# ── barrier / cluster-state (pure) ───────────────────────────────────────────


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


# ── workload primitives (per-substrate) ──────────────────────────────────────


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


def test_vm_move_file_uses_shutil_move_for_cross_device(mocker):
    """VM move_file uses shutil.move so cross-partition (data<->archive) moves work."""
    from src.workload_vm import ValkeyVmWorkload

    wl = ValkeyVmWorkload.__new__(ValkeyVmWorkload)
    move = mocker.patch("workload_vm.shutil.move")
    src = mocker.Mock(as_posix=lambda: "/data/dump.rdb")
    dest = mocker.Mock(as_posix=lambda: "/archive/dump.rdb.pre-restore")
    wl.move_file(src, dest)
    move.assert_called_once_with("/data/dump.rdb", "/archive/dump.rdb.pre-restore")


# ── sentinel manager ─────────────────────────────────────────────────────────


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


# ── backup manager: download / verify / restore primitives ───────────────────


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


def test_blocking_reason_blocks_backup_during_restore(mocker):
    """A restore in progress blocks create-backup but never the read-only list-backups."""
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
    assert ev._blocking_reason(check_running_operations=False) is None


# ── event-driven (ops.testing / Scenario) ────────────────────────────────────
#
# The restore action + state machine are driven through real Juju events so the
# peer-relation data-interface wiring is part of the test, per PR #79 review.


def _restore_context_and_state(cloud_spec, *, leader=True, app_data=None, unit_data=None):
    """Build a Context + single-unit State wired for the restore workflow."""
    from ops import testing

    from src.charm import ValkeyCharm
    from src.literals import PEER_RELATION, S3_RELATION_NAME, STATUS_PEERS_RELATION

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    # PeerModel serializes with hyphenated aliases, so app_data/unit_data keys
    # (and the reads below) use hyphens.
    peer = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_app_data=app_data or {},
        local_unit_data={"start-state": "started", **(unit_data or {})},
    )
    status_peer = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    s3_rel = testing.Relation(
        id=3, endpoint=S3_RELATION_NAME, interface="s3", remote_app_name="s3-integrator"
    )
    state = testing.State(
        model=testing.Model(name="m", type="lxd", cloud_spec=cloud_spec),
        leader=leader,
        relations={peer, status_peer, s3_rel},
        containers={testing.Container(name="valkey", can_connect=True)},
    )
    return ctx, state


def _peer_app_data(state):
    from src.literals import PEER_RELATION

    return next(r for r in state.relations if r.endpoint == PEER_RELATION).local_app_data


def _peer_unit_data(state):
    from src.literals import PEER_RELATION

    return next(r for r in state.relations if r.endpoint == PEER_RELATION).local_unit_data


@pytest.fixture
def restore_managers(mocker):
    """Patch every manager op the restore workflow drives, returning the mocks.

    These are the external/infra effects (Valkey/Sentinel/S3/workload). Patching
    them lets the Scenario tests assert orchestration and the resulting peer
    databag without a live cluster. ``is_primary`` defaults to True (this unit is
    the primary); flip it per-test for the replica branch.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        is_primary=mocker.patch("managers.cluster.ClusterManager.is_primary", return_value=True),
        wait_until_loaded=mocker.patch("managers.cluster.ClusterManager.wait_until_loaded"),
        wait_until_resynced=mocker.patch("managers.cluster.ClusterManager.wait_until_resynced"),
        verify_backup_is_rdb=mocker.patch("managers.backup.BackupManager.verify_backup_is_rdb"),
        restore_on_primary=mocker.patch("managers.backup.BackupManager.restore_on_primary"),
        download_backup=mocker.patch("managers.backup.BackupManager.download_backup"),
        cleanup_restore_files=mocker.patch("managers.backup.BackupManager.cleanup_restore_files"),
        roll_back=mocker.patch("managers.backup.BackupManager.roll_back"),
        suppress_failover=mocker.patch("managers.sentinel.SentinelManager.suppress_failover"),
        resume_failover=mocker.patch("managers.sentinel.SentinelManager.resume_failover"),
    )


def _pass_restore_preconditions(mocker, backups):
    """Satisfy the gates in ``_restore_blocking_reason``.

    ``s3_credentials`` is an ``ExtraSecretStr`` routed through a Juju secret
    (only presence is gated on here), so it's patched at the property rather than
    forged into the databag.
    """
    from unittest.mock import PropertyMock

    mocker.patch(
        "core.models.ValkeyCluster.s3_credentials",
        new_callable=PropertyMock,
        return_value=object(),  # truthy; the credentials themselves are unused here
    )
    mocker.patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.0.1")
    mocker.patch("managers.sentinel.SentinelManager.is_failover_in_progress", return_value=False)
    mocker.patch("managers.backup.BackupManager.list_backups", return_value=backups)


# ── restore action ───────────────────────────────────────────────────────────


def test_restore_action_initiates_workflow(mocker, cloud_spec, restore_managers):
    """`restore` on the leader validates, then writes the workflow's app-databag target."""
    from src.literals import RestoreStep

    _pass_restore_preconditions(mocker, ["2026-05-13T10:00:00Z"])
    ctx, state = _restore_context_and_state(cloud_spec)

    state_out = ctx.run(
        ctx.on.action("restore", params={"backup-id": "2026-05-13T10:00:00Z"}), state
    )

    app = _peer_app_data(state_out)
    assert app["restore-id"] == "2026-05-13T10:00:00Z"
    assert app["restore-instruction"] == RestoreStep.RESTORE.value
    assert app["restore-participants"] == "valkey/0"
    assert ctx.action_results == {"restore": "initiated for 2026-05-13T10:00:00Z"}
    # The action only *initiates*; the destructive work is the async workflow.
    restore_managers.restore_on_primary.assert_not_called()


def test_restore_action_rejected_on_non_leader(cloud_spec):
    """A follower must refuse restore and write nothing to the app databag."""
    from ops import testing

    ctx, state = _restore_context_and_state(cloud_spec, leader=False)
    with pytest.raises(testing.ActionFailed) as exc:
        ctx.run(ctx.on.action("restore", params={"backup-id": "2026-05-13T10:00:00Z"}), state)

    assert "leader" in exc.value.message.lower()
    assert _peer_app_data(exc.value.state).get("restore-id", "") == ""


def test_restore_action_rejects_unknown_backup_id(mocker, cloud_spec, restore_managers):
    """An unknown backup-id fails the action and never initiates the workflow."""
    from ops import testing

    _pass_restore_preconditions(mocker, ["2026-05-13T10:00:00Z"])
    ctx, state = _restore_context_and_state(cloud_spec)

    with pytest.raises(testing.ActionFailed) as exc:
        ctx.run(ctx.on.action("restore", params={"backup-id": "nope"}), state)

    assert "not found" in exc.value.message.lower()
    assert _peer_app_data(exc.value.state).get("restore-id", "") == ""


# ── restore workflow state machine ───────────────────────────────────────────


def test_single_unit_restore_completes_in_one_hook(cloud_spec, restore_managers):
    """A single-unit restore must finish (id + per-unit step cleared) in ONE hook.

    Without a peer to bounce relation_changed, a single-unit leader would
    otherwise crawl one step per ~5-min update_status. Driving the state machine
    to a fixed point inside the hook makes it complete on the first event.
    """
    ctx, state = _restore_context_and_state(
        cloud_spec,
        app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.RESTORE.value,
            "restore-participants": "valkey/0",
        },
    )

    state_out = ctx.run(ctx.on.update_status(), state)  # exactly ONE hook

    assert _peer_app_data(state_out).get("restore-id", "") == ""
    assert _peer_unit_data(state_out).get("restore-step", "") == ""


def test_primary_runs_full_restore_workflow(cloud_spec, restore_managers):
    """A single-unit primary drives RESTORE→RESYNC→COMPLETED and clears state, no rollback."""
    ctx, state = _restore_context_and_state(
        cloud_spec,
        app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.RESTORE.value,
            "restore-participants": "valkey/0",
        },
    )

    state_out = ctx.run(ctx.on.update_status(), state)

    restore_managers.suppress_failover.assert_called_once()  # RESTORE
    restore_managers.restore_on_primary.assert_called_once()  # RESTORE
    restore_managers.wait_until_loaded.assert_called_once()  # RESTORE post-check
    restore_managers.resume_failover.assert_called_once()  # RESYNC (primary)
    restore_managers.cleanup_restore_files.assert_called_once()  # COMPLETED
    restore_managers.roll_back.assert_not_called()
    assert _peer_app_data(state_out).get("restore-id", "") == ""
    assert _peer_unit_data(state_out).get("restore-step", "") == ""


def test_replica_records_role_and_barrier_holds(cloud_spec, restore_managers):
    """A replica records role/step but never restores; the barrier stalls on an absent peer."""
    restore_managers.is_primary.return_value = False
    ctx, state = _restore_context_and_state(
        cloud_spec,
        app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.RESTORE.value,
            # valkey/1 is a listed participant but absent -> fail-closed barrier.
            "restore-participants": "valkey/0,valkey/1",
        },
    )

    state_out = ctx.run(ctx.on.update_status(), state)

    restore_managers.restore_on_primary.assert_not_called()
    restore_managers.suppress_failover.assert_not_called()
    assert _peer_unit_data(state_out)["restore-role"] == "replica"
    assert _peer_unit_data(state_out)["restore-step"] == RestoreStep.RESTORE.value
    # Barrier not met (valkey/1 never reached RESTORE): still in progress, no advance.
    assert _peer_app_data(state_out)["restore-id"] == "2026-05-13T10:00:00Z"
    assert _peer_app_data(state_out)["restore-instruction"] == RestoreStep.RESTORE.value


def test_step_skipped_when_prior_not_reached(cloud_spec, restore_managers):
    """instruction=RESYNC but this unit never recorded RESTORE → it acts on nothing."""
    ctx, state = _restore_context_and_state(
        cloud_spec,
        app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.RESYNC.value,
            "restore-participants": "valkey/0",
        },
        # No restore-step recorded: this unit missed the fused RESTORE.
    )

    state_out = ctx.run(ctx.on.update_status(), state)

    restore_managers.wait_until_resynced.assert_not_called()
    restore_managers.resume_failover.assert_not_called()
    assert _peer_unit_data(state_out).get("restore-step", "") == ""
    # An out-of-step unit can never satisfy the barrier, so the instruction holds.
    assert _peer_app_data(state_out)["restore-instruction"] == RestoreStep.RESYNC.value


def test_bad_backup_tears_down_before_stopping_primary(cloud_spec, restore_managers):
    """A non-RDB backup fails the pre-stop check: the primary is never stopped or rolled back."""
    from common.exceptions import ValkeyRestoreError

    restore_managers.verify_backup_is_rdb.side_effect = ValkeyRestoreError("not an RDB")
    ctx, state = _restore_context_and_state(
        cloud_spec,
        app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.RESTORE.value,
            "restore-participants": "valkey/0",
        },
    )

    state_out = ctx.run(ctx.on.update_status(), state)

    restore_managers.restore_on_primary.assert_not_called()
    restore_managers.roll_back.assert_not_called()
    # Teardown still resumes the suppression it turned on before validating.
    restore_managers.resume_failover.assert_called_once()
    assert _peer_app_data(state_out).get("restore-id", "") == ""


def test_restore_failure_rolls_back_and_resumes_failover(cloud_spec, restore_managers):
    """A service failure mid-restore rolls back, resumes failover, flags FAILED, clears state.

    Regression for FIX 1: service-control errors are standalone Exception
    subclasses outside the restore-error hierarchy, so only the broad catch keeps
    them from escaping resume_failover().
    """
    from common.exceptions import ValkeyServicesFailedToStartError
    from src.statuses import RestoreStatuses

    restore_managers.restore_on_primary.side_effect = ValkeyServicesFailedToStartError("boom")
    ctx, state = _restore_context_and_state(
        cloud_spec,
        app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.RESTORE.value,
            "restore-participants": "valkey/0",
        },
    )

    with ctx(ctx.on.update_status(), state) as mgr:
        state_out = mgr.run()
        statuses = mgr.charm.state.statuses.get(
            scope="app", component="backup", running_status_only=True
        ).root

    restore_managers.roll_back.assert_called_once()
    restore_managers.resume_failover.assert_called_once()  # the critical invariant
    assert RestoreStatuses.RESTORE_FAILED.value in statuses
    assert _peer_app_data(state_out).get("restore-id", "") == ""


def test_restore_failure_unhealthy_status(cloud_spec, restore_managers):
    """A cluster-not-ready failure surfaces RESTORE_UNHEALTHY, not RESTORE_FAILED."""
    from common.exceptions import ValkeyClusterNotReadyError
    from src.statuses import RestoreStatuses

    restore_managers.wait_until_loaded.side_effect = ValkeyClusterNotReadyError("unhealthy")
    ctx, state = _restore_context_and_state(
        cloud_spec,
        app_data={
            "restore-id": "2026-05-13T10:00:00Z",
            "restore-instruction": RestoreStep.RESTORE.value,
            "restore-participants": "valkey/0",
        },
    )

    with ctx(ctx.on.update_status(), state) as mgr:
        mgr.run()
        statuses = mgr.charm.state.statuses.get(
            scope="app", component="backup", running_status_only=True
        ).root

    # The RDB swap succeeded but the server never came up healthy → roll back.
    restore_managers.roll_back.assert_called_once()
    assert RestoreStatuses.RESTORE_UNHEALTHY.value in statuses
    assert RestoreStatuses.RESTORE_FAILED.value not in statuses


# ── restore-awareness guards (single early-return clauses) ────────────────────
#
# These assert one guard clause on a handler that otherwise has nothing to do
# with restore; driving them through full events would entangle unrelated
# handlers (and re-run the restore workflow), so they stay at the unit layer.


def test_storage_detaching_refuses_during_restore(mocker):
    from src.events.base_events import BaseEvents

    ev = BaseEvents.__new__(BaseEvents)
    ev.charm = mocker.Mock()
    # Not already scaled down (the is_being_removed early-return runs first).
    ev.charm.state.unit_server.is_being_removed = False
    ev.charm.state.unit_server.is_backup_in_progress = False
    ev.charm.state.cluster.is_restore_in_progress = True

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
