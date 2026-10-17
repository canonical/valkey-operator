#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the persistence modes (DA290)."""

from dataclasses import replace
from pathlib import PurePosixPath
from unittest.mock import ANY, MagicMock, PropertyMock, call, patch

import pytest
from ops import testing
from pydantic import ValidationError

from common.client import ValkeyClient
from common.exceptions import (
    SentinelFailoverError,
    ValkeyCannotGetPrimaryIPError,
    ValkeyClusterNotReadyError,
    ValkeyServerRestartedError,
    ValkeyWorkloadCommandError,
)
from core.models import PersistenceConfig
from literals import AppendFsync, PersistenceMode
from managers.cluster import ClusterManager
from src.charm import ValkeyCharm
from src.literals import (
    PEER_RELATION,
    ROLLINGOPS_PEER_RELATION,
    STATUS_PEERS_RELATION,
    StartState,
)
from src.statuses import PersistenceStatuses

from .helpers import status_is


def test_defaults_match_the_previous_rdb_setup():
    """The defaults keep today's RDB behavior, so upgrades change nothing."""
    persistence = PersistenceConfig()

    assert persistence.mode == PersistenceMode.RDB
    assert persistence.rdb_save_points == "900 1 300 100 60 10000"
    assert persistence.aof_appendfsync == AppendFsync.EVERYSEC


@pytest.mark.parametrize(
    "mode,rdb,aof",
    [("RDB", True, False), ("AOF", False, True), ("RDB+AOF", True, True), ("None", False, False)],
)
def test_mode_flags(mode, rdb, aof):
    """Each mode enables exactly the persistence it names."""
    persistence = PersistenceConfig.from_charm_config({"persistence-mode": mode})

    assert (persistence.rdb_enabled, persistence.aof_enabled) == (rdb, aof)


@pytest.mark.parametrize("mode", ["AOF", "None"])
def test_save_points_are_ignored_outside_the_rdb_modes(mode):
    """A bad rdb-save-points can't block AOF or None, which don't use it."""
    persistence = PersistenceConfig.from_charm_config(
        {"persistence-mode": mode, "rdb-save-points": "900"}
    )

    assert persistence.mode == mode


@pytest.mark.parametrize(
    "config",
    [
        {"persistence-mode": "aof"},
        {"persistence-mode": "RDB AOF"},
        {"rdb-save-points": ""},
        {"rdb-save-points": "900"},
        {"rdb-save-points": "0 1"},
        {"rdb-save-points": "900 -1"},
        {"rdb-save-points": "900 one"},
        # Arabic-Indic digits pass isdecimal, but Valkey rejects them
        {"rdb-save-points": "\u0669\u0660\u0660 \u0661"},
        {"aof-appendfsync": "sometimes"},
    ],
)
def test_invalid_config_is_rejected(config):
    """Values outside DA290's allowed set fail validation."""
    with pytest.raises(ValidationError):
        PersistenceConfig.from_charm_config(config)


def test_save_points_whitespace_is_normalised():
    """Extra whitespace in rdb-save-points is collapsed."""
    persistence = PersistenceConfig.from_charm_config({"rdb-save-points": " 60  10 "})

    assert persistence.rdb_save_points == "60 10"


def _leader_state(config: dict, app_data: dict | None = None, unit_data: dict | None = None):
    relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_app_data=app_data or {},
        local_unit_data=unit_data or {},
    )
    status_peers = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    state = testing.State(
        leader=True,
        relations={relation, status_peers},
        containers={testing.Container(name="valkey", can_connect=True)},
        config=config,
        model=testing.Model(name="my-vm-model", type="lxd"),
    )
    return relation, state


def test_leader_stores_valid_persistence_config():
    """A valid config lands in the app databag for every unit to apply."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _leader_state({"persistence-mode": "AOF"})

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    stored = state_out.get_relation(relation.id).local_app_data["persistence-config"]
    assert PersistenceConfig.model_validate_json(stored).mode == PersistenceMode.AOF


def test_leader_stores_config_when_config_changed_defers():
    """Storing needs no unit address, so a failed address reconcile must not delay it."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _leader_state({"persistence-mode": "AOF"})

    with patch("events.base_events.BaseEvents._reconcile_unit_address", return_value=False):
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    stored = state_out.get_relation(relation.id).local_app_data["persistence-config"]
    assert PersistenceConfig.model_validate_json(stored).mode == PersistenceMode.AOF


def test_leader_applies_new_settings_in_config_changed():
    """The leader gets no relation-changed for its own app databag write, so it applies now."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _leader_state(
        {"persistence-mode": "AOF"}, unit_data={"start-state": StartState.STARTED.value}
    )

    with (
        patch("managers.cluster.ClusterManager.enable_new_persistence") as enable,
        patch("managers.cluster.ClusterManager.disable_old_persistence"),
        patch("managers.config.ConfigManager.set_config_properties"),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.0.9"),
    ):
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert enable.call_args.args[0]["appendonly"] == "yes"
    assert _applied(state_out, relation) == AOF


def test_leader_keeps_last_valid_config_and_blocks():
    """One invalid option rejects the whole change and blocks the app."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    previous = PersistenceConfig(mode=PersistenceMode.AOF)
    relation, state_in = _leader_state(
        {"persistence-mode": "aof", "rdb-save-points": "60 10"},
        app_data={"persistence-config": previous.model_dump_json()},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    stored = state_out.get_relation(relation.id).local_app_data["persistence-config"]
    assert PersistenceConfig.model_validate_json(stored) == previous
    assert status_is(state_out, PersistenceStatuses.INVALID_CONFIG.value, is_app=True)


def _cluster_manager(mocker) -> tuple[ClusterManager, MagicMock]:
    cm = ClusterManager(state=MagicMock(), workload=MagicMock())
    cm.state.endpoint = "10.0.0.1"
    client = MagicMock()
    mocker.patch.object(cm, "_get_valkey_client", return_value=client)
    return cm, client


AOF_SETTINGS = {
    "save": "",
    "appendonly": "yes",
    "appendfsync": "everysec",
    "rdb-del-sync-files": "no",
    "shutdown-on-sigterm": "default",
}
RDB_SETTINGS = {
    "save": "900 1",
    "appendonly": "no",
    "appendfsync": "everysec",
    "rdb-del-sync-files": "no",
    "shutdown-on-sigterm": "default",
}


def test_rdb_to_aof_keeps_rdb_on_until_disable(mocker):
    """RDB to AOF: enabling turns AOF on and waits, only disabling drops the save points."""
    cm, client = _cluster_manager(mocker)
    mocker.patch.object(cm, "_is_aof_on", return_value=True)
    client.info_persistence.return_value = {"aof_enabled": "1"}

    cm.enable_new_persistence(AOF_SETTINGS)

    assert client.config_set.call_args_list == [
        call({"appendonly": "yes"}, hostname="10.0.0.1"),
        call({"appendfsync": "everysec", "shutdown-on-sigterm": "default"}, hostname="10.0.0.1"),
    ]

    client.config_set.reset_mock()
    cm.disable_old_persistence(AOF_SETTINGS)

    client.config_set.assert_called_once_with(
        {"rdb-del-sync-files": "no", "save": ""}, hostname="10.0.0.1"
    )


def test_aof_to_rdb_saves_rdb_and_keeps_aof_on_until_disable(mocker):
    """AOF to RDB: enabling makes dump.rdb current, only disabling turns AOF off."""
    cm, client = _cluster_manager(mocker)
    client.info_persistence.return_value = {"aof_enabled": "1"}
    order = MagicMock()
    order.attach_mock(client.config_set, "config_set")
    order.attach_mock(mocker.patch.object(cm, "_save_in_background"), "save")

    cm.enable_new_persistence(RDB_SETTINGS)

    assert order.mock_calls == [
        call.config_set(
            {"appendfsync": "everysec", "shutdown-on-sigterm": "default", "save": "900 1"},
            hostname="10.0.0.1",
        ),
        call.save(ANY),
    ]

    client.config_set.reset_mock()
    cm.disable_old_persistence(RDB_SETTINGS)

    client.config_set.assert_called_once_with(
        {"rdb-del-sync-files": "no", "appendonly": "no"}, hostname="10.0.0.1"
    )


def test_aof_wait_fails_at_once_when_valkey_restarts(mocker):
    """A restart during the AOF rewrite loses the runtime appendonly, so the wait fails at once."""
    cm, client = _cluster_manager(mocker)
    mocker.patch("managers.cluster.DATASET_IO_TIMEOUT_S", 1)
    client.info_server.side_effect = [{"run_id": "before"}, {"run_id": "after"}]
    client.info_persistence.return_value = {
        "aof_enabled": "0",
        "aof_rewrite_in_progress": "0",
        "aof_rewrite_scheduled": "0",
    }

    with pytest.raises(ValkeyServerRestartedError):
        cm.enable_new_persistence(AOF_SETTINGS)


def test_bgsave_wait_fails_when_valkey_restarts(mocker):
    """A restarted server reports no BGSAVE and a default ok status, which prove nothing."""
    cm, client = _cluster_manager(mocker)
    mocker.patch("managers.cluster.DATASET_IO_TIMEOUT_S", 1)
    client.info_server.side_effect = [
        {"run_id": "before"},
        {"run_id": "before"},
        ValkeyWorkloadCommandError("Connection refused"),
        {"run_id": "after"},
    ]
    client.info_persistence.return_value = {
        "aof_enabled": "1",
        "aof_rewrite_in_progress": "0",
        "aof_rewrite_scheduled": "0",
        "rdb_bgsave_in_progress": "0",
        "rdb_last_bgsave_status": "ok",
    }

    with pytest.raises(ValkeyServerRestartedError):
        cm.enable_new_persistence(RDB_SETTINGS)


def _started_unit_state(app_data: dict, unit_data: dict, peers_data: dict | None = None):
    relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_app_data=app_data,
        local_unit_data={"start-state": StartState.STARTED.value, **unit_data},
        peers_data=peers_data or {},
    )
    status_peers = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    state = testing.State(
        leader=False,
        relations={
            relation,
            status_peers,
            testing.PeerRelation(id=100, endpoint=ROLLINGOPS_PEER_RELATION),
        },
        containers={testing.Container(name="valkey", can_connect=True)},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )
    return relation, state


AOF = PersistenceConfig(mode=PersistenceMode.AOF)
AOF_JSON = AOF.model_dump_json()


def _applied(state_out, relation) -> PersistenceConfig | None:
    unit_data = state_out.get_relation(relation.id).local_unit_data
    if "applied-persistence-config" not in unit_data:
        return None
    return PersistenceConfig.model_validate_json(unit_data["applied-persistence-config"])


@pytest.mark.parametrize("live_matches", [True, False])
@pytest.mark.parametrize(
    "stored",
    # AOF mode doesn't use save points, so this edit changes no Valkey setting
    [
        AOF_JSON,
        PersistenceConfig(mode=PersistenceMode.AOF, rdb_save_points="60 1").model_dump_json(),
    ],
)
def test_reconcile_skips_only_when_applied_and_live(live_matches, stored):
    """The applied mode is applied again when the live server drifted."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    _, state_in = _started_unit_state(
        {"persistence-config": stored}, {"applied-persistence-config": AOF_JSON}
    )

    with (
        patch("managers.cluster.ClusterManager.is_live_persistence", return_value=live_matches),
        patch("managers.cluster.ClusterManager.enable_new_persistence") as enable,
        patch("managers.cluster.ClusterManager.disable_old_persistence"),
        patch("managers.config.ConfigManager.set_config_properties"),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.0.9"),
    ):
        ctx.run(ctx.on.update_status(), state_in)

    assert enable.called is not live_matches


def test_reconcile_turns_new_on_renders_then_turns_old_off():
    """Whichever mode the file names on a restart, the server keeps writing it until then."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _started_unit_state({"persistence-config": AOF_JSON}, {})

    with (
        patch("managers.cluster.ClusterManager.enable_new_persistence") as enable,
        patch("managers.config.ConfigManager.set_config_properties") as render,
        patch("managers.cluster.ClusterManager.disable_old_persistence") as disable,
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.0.9"),
    ):
        order = MagicMock()
        order.attach_mock(enable, "enable")
        order.attach_mock(render, "render")
        order.attach_mock(disable, "disable")
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert [entry[0] for entry in order.mock_calls] == ["enable", "render", "disable"]
    assert enable.call_args.args[0]["appendonly"] == "yes"
    render.assert_called_once_with(primary_endpoint="10.0.0.9", persistence=AOF)
    assert _applied(state_out, relation) == AOF


def test_failed_render_keeps_old_persistence_on():
    """A file still naming the old mode needs the old persistence live, so nothing turns off."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _started_unit_state({"persistence-config": AOF_JSON}, {})

    with (
        patch("managers.cluster.ClusterManager.enable_new_persistence"),
        patch(
            "managers.config.ConfigManager.set_config_properties",
            side_effect=ValkeyWorkloadCommandError("disk full"),
        ),
        patch("managers.cluster.ClusterManager.disable_old_persistence") as disable,
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.0.9"),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    disable.assert_not_called()
    assert _applied(state_out, relation) is None
    assert status_is(state_out, PersistenceStatuses.NOT_APPLIED.value)


def test_reconcile_applies_defaults_after_upgrade():
    """A unit upgraded from a revision without the new fields applies the defaults once."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _started_unit_state({}, {})

    with (
        patch("managers.cluster.ClusterManager.enable_new_persistence") as enable,
        patch("managers.cluster.ClusterManager.disable_old_persistence"),
        patch("managers.config.ConfigManager.set_config_properties"),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.0.9"),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert enable.call_args.args[0]["appendonly"] == "no"
    assert _applied(state_out, relation) == PersistenceConfig()


def test_reconcile_timeout_keeps_marker():
    """A rewrite that does not finish leaves the file and the marker untouched."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _started_unit_state({"persistence-config": AOF_JSON}, {})

    with (
        patch(
            "managers.cluster.ClusterManager.enable_new_persistence",
            side_effect=ValkeyClusterNotReadyError("AOF rewrite did not finish"),
        ),
        patch("managers.config.ConfigManager.set_config_properties") as render,
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.0.9"),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    render.assert_not_called()
    assert _applied(state_out, relation) is None


def test_reconcile_needs_the_primary_before_it_changes_the_server():
    """Without the primary the file can't be rendered, so the live server must stay as is."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _started_unit_state({"persistence-config": AOF_JSON}, {})

    with (
        patch("managers.cluster.ClusterManager.enable_new_persistence") as enable,
        patch(
            "managers.sentinel.SentinelManager.get_primary_ip",
            side_effect=ValkeyCannotGetPrimaryIPError("no sentinel"),
        ),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    enable.assert_not_called()
    assert _applied(state_out, relation) is None


def test_failed_reconcile_does_not_defer_relation_changed():
    """update-status retries the reconcile, so a deferred event would only replay the wait."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _started_unit_state(
        {"persistence-config": AOF_JSON}, {}, peers_data={1: {}}
    )

    with (
        patch(
            "managers.cluster.ClusterManager.enable_new_persistence",
            side_effect=ValkeyClusterNotReadyError("AOF rewrite did not finish"),
        ),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="10.0.0.9"),
    ):
        state_out = ctx.run(ctx.on.relation_changed(relation, remote_unit=1), state_in)

    assert not [e for e in state_out.deferred if "BaseEvents" in e.owner]


def test_new_leader_stores_the_config():
    """A new leader stores the config, since the old one may have missed config-changed."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation, state_in = _started_unit_state({}, {})
    state_in = replace(state_in, leader=True, config={"persistence-mode": "AOF"})

    with patch("events.base_events.BaseEvents._ensure_workload_ready"):
        state_out = ctx.run(ctx.on.leader_elected(), state_in)

    app_data = state_out.get_relation(relation.id).local_app_data
    assert PersistenceConfig.model_validate_json(app_data["persistence-config"]).mode == "AOF"


def test_config_set_raises_on_an_error_reply():
    """valkey-cli prints a refused CONFIG SET on stdout with exit code 0."""
    workload = MagicMock()
    workload.exec.return_value = ("ERR CONFIG SET failed (possibly related to argument 'x')", "")
    client = ValkeyClient(username="u", password="p", tls=False, workload=workload)

    with pytest.raises(ValkeyWorkloadCommandError):
        client.config_set({"appendonly": "yes"}, hostname="10.0.0.1")


def test_shutdown_save_waits_for_aof_rewrite(mocker):
    """A planned shutdown waits out a running AOF rewrite before it saves."""
    cm, _ = _cluster_manager(mocker)
    order = MagicMock()
    order.attach_mock(
        mocker.patch.object(cm, "_is_aof_rewrite_idle", side_effect=[False, True]), "idle"
    )
    order.attach_mock(mocker.patch.object(cm, "_save_database_blocking"), "save")
    mocker.patch.object(cm, "_disable_save_on_shutdown")

    cm.save_dataset_before_shutdown()

    assert [entry[0] for entry in order.mock_calls] == ["idle", "idle", "save"]


START = "oO0OoO0OoO0Oo Valkey is starting oO0OoO0OoO0Oo"
READY = "Ready to accept connections tls"
FATAL = "Bad file format reading the append only file appendonly.aof.3.incr.aof: make a backup"


@pytest.mark.parametrize(
    "responding,manifest_exists,log,corrupted",
    [
        # a PONG or a LOADING reply
        (True, True, f"{START}\n{FATAL}", False),
        (False, False, f"{START}\n{FATAL}", False),
        # down between restarts, the last start was fine
        (False, True, f"{START}\n{READY}", False),
        (False, True, f"{START}\n{FATAL}\n{START}\n{READY}", False),
        (False, True, f"{START}\n{READY}\n{START}\n{FATAL}", True),
    ],
)
def test_is_aof_corrupted(mocker, responding, manifest_exists, log, corrupted):
    """Only a silent server whose last start logged a fatal AOF error counts as corrupted."""
    cm, client = _cluster_manager(mocker)
    client.is_responding.return_value = responding
    cm.workload.path_exists.return_value = manifest_exists
    cm.workload.read_file_tail.return_value = log

    assert cm.is_aof_corrupted() is corrupted
    if responding:
        cm.workload.read_file_tail.assert_not_called()


def test_delete_corrupted_aof_removes_the_manifest_first(mocker):
    """The manifest goes first, so a partial delete leaves files Valkey ignores."""
    cm, _ = _cluster_manager(mocker)
    cm.workload.working_dir = PurePosixPath("/data")

    cm.delete_corrupted_aof()

    removed = [
        c.args[0].as_posix()
        for c in cm.workload.mock_calls
        if c[0] in ("remove_dir", "remove_file")
    ]
    assert removed == ["/data/appendonlydir/appendonly.aof.manifest", "/data/appendonlydir"]


OWN_ENDPOINT = "10.0.0.1"
NEW_PRIMARY = "10.0.0.9"


def _run_heal(
    primary_ips: list[str],
    failover_error: Exception | None = None,
    delete_error: Exception | None = None,
    primary_healthy: bool = True,
):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    _, state_in = _started_unit_state({}, {})
    with (
        patch(
            "core.cluster_state.ClusterState.endpoint",
            new_callable=PropertyMock,
            return_value=OWN_ENDPOINT,
        ),
        patch("managers.cluster.ClusterManager.is_aof_corrupted", return_value=True),
        patch("managers.sentinel.SentinelManager.get_primary_ip", side_effect=primary_ips),
        patch(
            "managers.sentinel.SentinelManager.failover", side_effect=failover_error
        ) as failover,
        patch("managers.config.ConfigManager.set_config_properties") as render,
        patch(
            "managers.cluster.ClusterManager.delete_corrupted_aof", side_effect=delete_error
        ) as delete,
        patch("managers.cluster.ClusterManager.is_healthy", return_value=primary_healthy),
        patch("events.base_events.BaseEvents.reconcile_persistence"),
        patch("src.charm.ValkeyCharm.request_restart") as restart,
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)
    return state_out, failover, render, delete, restart


def test_heal_resyncs_a_replica_from_the_primary():
    """Sentinel already failed over: render as replica and delete the AOF, no charm restart."""
    _, failover, render, delete, restart = _run_heal([NEW_PRIMARY])

    failover.assert_not_called()
    render.assert_called_once_with(primary_endpoint=NEW_PRIMARY)
    delete.assert_called_once()
    # a second restart would abort the full sync
    restart.assert_not_called()


def test_heal_forces_a_failover_when_still_primary():
    """Sentinel still names this unit: force the failover, then heal as a replica."""
    _, failover, render, delete, _ = _run_heal([OWN_ENDPOINT, NEW_PRIMARY])

    failover.assert_called_once_with(force=True)
    render.assert_called_once_with(primary_endpoint=NEW_PRIMARY)
    delete.assert_called_once()


def test_heal_blocks_without_a_healthy_peer():
    """No replica to promote: block and keep the AOF in place."""
    state_out, _, render, delete, restart = _run_heal(
        [OWN_ENDPOINT], failover_error=SentinelFailoverError("NOGOODSLAVE")
    )

    render.assert_not_called()
    delete.assert_not_called()
    restart.assert_not_called()
    assert status_is(state_out, PersistenceStatuses.AOF_CORRUPTED.value)


def test_heal_waits_for_a_primary_that_answers():
    """No heal from a down primary, which would leave this unit empty and promotable."""
    state_out, _, render, delete, restart = _run_heal([NEW_PRIMARY], primary_healthy=False)

    render.assert_not_called()
    delete.assert_not_called()
    restart.assert_not_called()
    assert status_is(state_out, PersistenceStatuses.AOF_CORRUPTED.value)


def test_heal_shows_a_status_when_the_delete_fails():
    """A failed delete shows a status, and the next update-status retries."""
    state_out, _, _, _, restart = _run_heal(
        [NEW_PRIMARY], delete_error=ValkeyWorkloadCommandError("rm failed")
    )

    restart.assert_not_called()
    assert status_is(state_out, PersistenceStatuses.AOF_HEAL_FAILED.value)
