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
