#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from ops import ActiveStatus, pebble, testing

from common.exceptions import ValkeyWorkloadCommandError
from src.charm import ValkeyCharm
from src.literals import (
    INTERNAL_USERS_PASSWORD_CONFIG,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    PEER_RELATION,
    STATUS_PEERS_RELATION,
    CharmUsers,
)
from src.statuses import CharmStatuses, ClusterStatuses

from .helpers import status_is

CHARM_USER = "_daemon_"
CONTAINER = "valkey"
SERVICE_VALKEY = "valkey"
SERVICE_METRIC_EXPORTER = "metric_exporter"
SERVICE_SENTINEL = "valkey-sentinel"

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]

internal_passwords_secret = testing.Secret(
    tracked_content={f"{user.value}-password": "secure-password" for user in CharmUsers},
    owner="app",
    label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL_SUFFIX}",
)


def test_start_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    # happy path
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
    )

    expected_plan = {
        "services": {
            SERVICE_VALKEY: {
                "override": "replace",
                "summary": "Valkey service",
                "command": "valkey-server /var/lib/valkey/valkey.conf",
                "user": CHARM_USER,
                "group": CHARM_USER,
                "startup": "enabled",
            },
            SERVICE_SENTINEL: {
                "override": "replace",
                "summary": "Valkey sentinel service",
                "command": "valkey-sentinel /var/lib/valkey/sentinel.conf",
                "user": CHARM_USER,
                "group": CHARM_USER,
                "startup": "enabled",
            },
            SERVICE_METRIC_EXPORTER: {
                "override": "replace",
                "summary": "Valkey metric exporter",
                "command": "bin/redis_exporter",
                "user": CHARM_USER,
                "group": CHARM_USER,
                "startup": "enabled",
            },
        }
    }

    # generate passwords
    state_out = ctx.run(ctx.on.leader_elected(), state_in)

    # start event
    state_out = ctx.run(ctx.on.start(), state_out)
    assert state_out.get_container(container.name).plan == expected_plan
    assert (
        state_out.get_container(container.name).service_statuses[SERVICE_VALKEY]
        == pebble.ServiceStatus.ACTIVE
    )
    assert (
        state_out.get_container(container.name).service_statuses[SERVICE_METRIC_EXPORTER]
        == pebble.ServiceStatus.ACTIVE
    )
    assert state_out.unit_status == ActiveStatus()
    assert state_out.app_status == ActiveStatus()

    # container not ready
    container = testing.Container(name=CONTAINER, can_connect=False)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
    )

    state_out = ctx.run(ctx.on.start(), state_in)
    assert status_is(state_out, CharmStatuses.SERVICE_NOT_STARTED.value)
    assert status_is(state_out, CharmStatuses.SERVICE_NOT_STARTED.value, is_app=True)


def test_start_non_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=False,
        relations={relation, status_peer_relation},
        containers={container},
    )

    with patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="127.1.0.1"):
        state_out = ctx.run(ctx.on.start(), state_in)
        assert not state_out.get_container(container.name).service_statuses.get(SERVICE_VALKEY)
        assert not state_out.get_container(container.name).service_statuses.get(
            SERVICE_METRIC_EXPORTER
        )
        assert "start" in [e.name for e in state_out.deferred]

        relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
        state_in = testing.State(
            model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
            leader=False,
            relations={relation, status_peer_relation},
            secrets={internal_passwords_secret},
            containers={container},
        )
        state_out = ctx.run(ctx.on.start(), state_in)

        assert status_is(state_out, ClusterStatuses.WAITING_FOR_PRIMARY_START.value)

        relation = testing.PeerRelation(
            id=1,
            endpoint=PEER_RELATION,
            local_app_data={"primary-ip": "127.1.0.1"},
            peers_data={1: {"start-state": "started"}},
        )
        state_in = testing.State(
            model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
            leader=False,
            relations={relation, status_peer_relation},
            secrets={internal_passwords_secret},
            containers={container},
        )
        state_out = ctx.run(ctx.on.start(), state_in)

        assert status_is(state_out, CharmStatuses.WAITING_TO_START.value)

        # replica syncing
        with patch("managers.cluster.ClusterManager.is_replica_synced", return_value=False):
            relation = testing.PeerRelation(
                id=1,
                endpoint=PEER_RELATION,
                local_app_data={"starting-member": "valkey/0"},
                peers_data={1: {"start-state": "started"}},
            )
            state_in = testing.State(
                model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
                leader=False,
                relations={relation, status_peer_relation},
                secrets={internal_passwords_secret},
                containers={container},
            )
            state_out = ctx.run(ctx.on.start(), state_in)
            assert status_is(state_out, ClusterStatuses.WAITING_FOR_REPLICA_SYNC.value)

        # sentinel not yet discovered
        with patch("managers.sentinel.SentinelManager.is_sentinel_discovered", return_value=False):
            relation = testing.PeerRelation(
                id=1,
                endpoint=PEER_RELATION,
                local_app_data={"starting-member": "valkey/0"},
                peers_data={1: {"start-state": "started"}},
            )
            state_in = testing.State(
                model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
                leader=False,
                relations={relation, status_peer_relation},
                secrets={internal_passwords_secret},
                containers={container},
            )
            state_out = ctx.run(ctx.on.start(), state_in)
            assert status_is(state_out, ClusterStatuses.WAITING_FOR_SENTINEL_DISCOVERY.value)

        # Happy path with sentinel discovered and replica synced
        with (
            patch("managers.sentinel.SentinelManager.is_sentinel_discovered", return_value=True),
            patch("managers.cluster.ClusterManager.is_replica_synced", return_value=True),
        ):
            relation = testing.PeerRelation(
                id=1,
                endpoint=PEER_RELATION,
                local_app_data={"starting-member": "valkey/0"},
                peers_data={1: {"start-state": "started"}},
            )
            state_in = testing.State(
                model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
                leader=False,
                relations={relation, status_peer_relation},
                secrets={internal_passwords_secret},
                containers={container},
            )
            state_out = ctx.run(ctx.on.start(), state_in)
            assert status_is(state_out, CharmStatuses.ACTIVE_IDLE.value)

            assert state_out.get_container(container.name).service_statuses.get(SERVICE_VALKEY)
            assert state_out.get_container(container.name).service_statuses.get(
                SERVICE_METRIC_EXPORTER
            )
            assert state_out.get_container(container.name).service_statuses[SERVICE_SENTINEL]
            assert state_out.get_relation(1).local_unit_data["start-state"] == "started"


def test_update_status_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    # happy path
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
    )

    state_out = ctx.run(ctx.on.update_status(), state_in)
    assert state_out.unit_status == ActiveStatus()


def test_update_status_non_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(
        id=1, endpoint=PEER_RELATION, local_unit_data={"start-state": "started"}
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=False,
        relations={relation, status_peer_relation},
        containers={container},
    )
    state_out = ctx.run(ctx.on.update_status(), state_in)
    assert state_out.unit_status == ActiveStatus()


def test_internal_user_creation(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        relations={relation},
        leader=True,
        containers={container},
    )
    state_out = ctx.run(ctx.on.leader_elected(), state_in)
    secret_out = state_out.get_secret(
        label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL_SUFFIX}"
    )
    assert secret_out.latest_content.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")


def test_leader_elected_no_peer_relation(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    state_out = ctx.run(ctx.on.leader_elected(), state_in)
    assert "leader_elected" in [e.name for e in state_out.deferred]


def test_leader_elected_leader_password_specified(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        tracked_content={CharmUsers.VALKEY_ADMIN.value: "secure-password"}, remote_grants=APP_NAME
    )
    state_in = testing.State(
        leader=True,
        relations={relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch(
            "managers.config.ConfigManager.generate_password", return_value="generated-password"
        ),
    ):
        state_out = ctx.run(ctx.on.leader_elected(), state_in)
        secret_out = state_out.get_secret(
            label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL_SUFFIX}"
        )
        for user in CharmUsers:
            if user == CharmUsers.VALKEY_ADMIN:
                assert secret_out.latest_content.get(f"{user.value}-password") == "secure-password"
                continue
            assert secret_out.latest_content.get(f"{user.value}-password") == "generated-password"


def test_leader_elected_leader_password_specified_wrong_secret(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={relation, status_relation},
        containers={container},
        config={INTERNAL_USERS_PASSWORD_CONFIG: "secret:1tf1wk0tmfrodp8ofwxn"},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with pytest.raises(testing.errors.UncaughtCharmError) as exc_info:
        ctx.run(ctx.on.leader_elected(), state_in)
        assert "SecretNotFoundError" in str(exc_info.value)


def test_config_changed_non_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)
    password_secret = testing.Secret(
        tracked_content={CharmUsers.VALKEY_ADMIN.value: "secure-password"}, remote_grants=APP_NAME
    )

    state_in = testing.State(
        leader=False,
        relations={relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch("events.base_events.BaseEvents._update_internal_users_password") as mock_update,
    ):
        ctx.run(ctx.on.config_changed(), state_in)
        mock_update.assert_not_called()


def test_config_changed_leader_unit_valkey_update_fails(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        tracked_content={user.value: "secure-password" for user in CharmUsers},
        remote_grants=APP_NAME,
    )
    state_in = testing.State(
        leader=True,
        relations={relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with patch("core.models.RelationState.update") as mock_update:
        ctx.run(ctx.on.config_changed(), state_in)
        mock_update.assert_called_once()


def test_config_changed_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        tracked_content={user.value: "secure-password" for user in CharmUsers},
        remote_grants=APP_NAME,
    )
    state_in = testing.State(
        leader=True,
        relations={relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch("managers.config.ConfigManager.set_acl_file") as mock_set_acl_file,
        patch("common.client.ValkeyClient.exec_cli_command") as mock_exec_command,
    ):
        state_out = ctx.run(ctx.on.config_changed(), state_in)
        mock_set_acl_file.assert_called_once()
        assert mock_exec_command.call_count == 2  # one for acl load, one for primaryauth set
        secret_out = state_out.get_secret(
            label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL_SUFFIX}"
        )
        assert (
            secret_out.latest_content.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
            == "secure-password"
        )


def test_config_changed_leader_unit_primary(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        tracked_content={user.value: "secure-password" for user in CharmUsers},
        remote_grants=APP_NAME,
    )
    state_in = testing.State(
        leader=True,
        relations={relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch("managers.config.ConfigManager.set_acl_file") as mock_set_acl_file,
        patch("common.client.ValkeyClient.exec_cli_command") as mock_exec_command,
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="127.0.1.1"),
    ):
        state_out = ctx.run(ctx.on.config_changed(), state_in)
        mock_set_acl_file.assert_called_once()
        assert mock_exec_command.call_count == 2  # one for acl load, one for primaryauth set
        secret_out = state_out.get_secret(
            label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL_SUFFIX}"
        )
        assert (
            secret_out.latest_content.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
            == "secure-password"
        )


def test_config_changed_leader_unit_wrong_username(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        tracked_content={"wrong-username": "secure-password"}, remote_grants=APP_NAME
    )
    state_in = testing.State(
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch("managers.config.ConfigManager.set_acl_file") as mock_set_acl_file,
        ctx(ctx.on.config_changed(), state_in) as manager,
    ):
        charm: ValkeyCharm = manager.charm
        manager.run()
        cluster_statuses = charm.state.statuses.get(
            scope="app",
            component=charm.cluster_manager.name,
        )
        assert ClusterStatuses.PASSWORD_UPDATE_FAILED.value in cluster_statuses
        mock_set_acl_file.assert_not_called()


def test_change_password_secret_changed_non_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "private-ip": "127.0.1.0"},
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL_SUFFIX}",
        tracked_content={CharmUsers.VALKEY_ADMIN.value: "secure-password"},
        remote_grants=APP_NAME,
    )

    state_in = testing.State(
        leader=False,
        relations={relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch(
            "events.base_events.BaseEvents._update_internal_users_password"
        ) as mock_update_password,
        patch("managers.config.ConfigManager.set_acl_file") as mock_set_acl_file,
        patch("common.client.ValkeyClient.exec_cli_command") as mock_exec_command,
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="127.0.1.1"),
    ):
        ctx.run(ctx.on.secret_changed(password_secret), state_in)
        mock_update_password.assert_not_called()
        mock_set_acl_file.assert_called_once()
        assert mock_exec_command.call_count == 2


def test_change_password_secret_changed_non_leader_unit_not_successful(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    statuses_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL_SUFFIX}",
        tracked_content={CharmUsers.VALKEY_ADMIN.value: "secure-password"},
        remote_grants=APP_NAME,
    )

    state_in = testing.State(
        leader=False,
        relations={relation, statuses_peer_relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch(
            "events.base_events.BaseEvents._update_internal_users_password"
        ) as mock_update_password,
        patch("managers.config.ConfigManager.set_acl_file") as mock_set_acl_file,
        patch(
            "common.client.ValkeyClient.exec_cli_command",
            side_effect=ValkeyWorkloadCommandError("Failed to execute command"),
        ) as mock_exec_command,
        ctx(ctx.on.secret_changed(password_secret), state_in) as manager,
    ):
        charm: ValkeyCharm = manager.charm
        state_out = manager.run()
        mock_update_password.assert_not_called()
        mock_set_acl_file.assert_called_once()
        mock_exec_command.assert_called_once_with(["acl", "load"], hostname="127.1.1.1")
        cluster_statuses = charm.state.statuses.get(
            scope="unit",
            component=charm.cluster_manager.name,
        )
        assert "secret_changed" in [e.name for e in state_out.deferred]
        assert ClusterStatuses.PASSWORD_UPDATE_FAILED.value in cluster_statuses


def test_change_password_secret_changed_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        tracked_content={CharmUsers.VALKEY_ADMIN.value: "secure-password"}, remote_grants=APP_NAME
    )

    state_in = testing.State(
        leader=True,
        relations={relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch(
            "events.base_events.BaseEvents._update_internal_users_password"
        ) as mock_update_password,
    ):
        ctx.run(ctx.on.secret_changed(password_secret), state_in)
        mock_update_password.assert_called_once_with(password_secret.id)
