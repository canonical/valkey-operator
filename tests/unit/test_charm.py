#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import patch

import yaml
from ops import ActiveStatus, pebble, testing

from src.charm import ValkeyCharm
from src.literals import (
    INTERNAL_USERS_PASSWORD_CONFIG,
    INTERNAL_USERS_SECRET_LABEL,
    PEER_RELATION,
    STATUS_PEERS_RELATION,
    CharmUsers,
)
from src.statuses import CharmStatuses

from .helpers import status_is

CHARM_USER = "valkey"
CONTAINER = "valkey"
SERVICE_VALKEY = "valkey"
SERVICE_METRIC_EXPORTER = "metric_exporter"

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


def test_pebble_ready_leader_unit(cloud_spec):
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

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)
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
    assert status_is(state_out, CharmStatuses.SCALING_NOT_IMPLEMENTED.value, is_app=True)

    # container not ready
    container = testing.Container(name=CONTAINER, can_connect=False)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
    )

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)
    assert status_is(state_out, CharmStatuses.SERVICE_NOT_STARTED.value)
    assert status_is(state_out, CharmStatuses.SERVICE_NOT_STARTED.value, is_app=True)


def test_pebble_ready_non_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    # happy path
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=False,
        relations={relation, status_peer_relation},
        containers={container},
    )

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)
    assert not state_out.get_container(container.name).service_statuses.get(SERVICE_VALKEY)
    assert not state_out.get_container(container.name).service_statuses.get(
        SERVICE_METRIC_EXPORTER
    )
    assert status_is(state_out, CharmStatuses.SCALING_NOT_IMPLEMENTED.value)

    # container not ready
    container = testing.Container(name=CONTAINER, can_connect=False)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
    )

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)
    assert status_is(state_out, CharmStatuses.SERVICE_NOT_STARTED.value)


def test_update_status_leader_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"started": "True"},
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
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
        leader=False,
        relations={relation, status_peer_relation},
        containers={container},
    )
    state_out = ctx.run(ctx.on.update_status(), state_in)
    assert status_is(state_out, CharmStatuses.SCALING_NOT_IMPLEMENTED.value)


def test_internal_user_creation():
    ctx = testing.Context(ValkeyCharm)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(relations={relation}, leader=True, containers={container})
    with patch("workload_k8s.ValkeyK8sWorkload.write_file"):
        state_out = ctx.run(ctx.on.leader_elected(), state_in)
        secret_out = state_out.get_secret(
            label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL}"
        )
        assert secret_out.latest_content.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")


def test_leader_elected_no_peer_relation():
    ctx = testing.Context(ValkeyCharm)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(leader=True, containers={container})
    with patch("workload_k8s.ValkeyK8sWorkload.write_file"):
        state_out = ctx.run(ctx.on.leader_elected(), state_in)
        assert "leader_elected" in [e.name for e in state_out.deferred]


def test_leader_elected_leader_password_specified():
    ctx = testing.Context(ValkeyCharm)
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
    )
    with (
        patch("workload_k8s.ValkeyK8sWorkload.write_file"),
        patch(
            "managers.config.ConfigManager.generate_password", return_value="generated-password"
        ),
    ):
        state_out = ctx.run(ctx.on.leader_elected(), state_in)
        secret_out = state_out.get_secret(label=f"{PEER_RELATION}.{APP_NAME}.app")
        assert (
            secret_out.latest_content.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
            == "secure-password"
        )
        secret_out = state_out.get_secret(label=f"{PEER_RELATION}.{APP_NAME}.app")
        for user in CharmUsers:
            if user == CharmUsers.VALKEY_ADMIN:
                assert secret_out.latest_content.get(f"{user.value}-password") == "secure-password"
                continue
            assert secret_out.latest_content.get(f"{user.value}-password") == "generated-password"


def test_leader_elected_leader_password_specified_wrong_secret():
    ctx = testing.Context(ValkeyCharm)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={relation, status_relation},
        containers={container},
        config={INTERNAL_USERS_PASSWORD_CONFIG: "secret:1tf1wk0tmfrodp8ofwxn"},
    )
    with (
        patch("workload_k8s.ValkeyK8sWorkload.write_file"),
        ctx(ctx.on.leader_elected(), state_in) as manager,
    ):
        charm: ValkeyCharm = manager.charm
        manager.run()
        assert (
            charm.state.statuses.get(scope="app", component="cluster")[0]
            == CharmStatuses.SECRET_ACCESS_ERROR.value
        )


def test_config_changed_non_leader_unit():
    ctx = testing.Context(ValkeyCharm)
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
    )
    with (
        patch("events.base_events.BaseEvents.update_admin_password") as mock_update,
    ):
        ctx.run(ctx.on.config_changed(), state_in)
        mock_update.assert_not_called()


def test_config_changed_leader_unit_valkey_update_fails():
    ctx = testing.Context(ValkeyCharm)
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
    )
    with (
        patch("workload_k8s.ValkeyK8sWorkload.write_file"),
        patch("common.client.ValkeyClient.create_client", side_effect=Exception("fail")),
        patch("core.models.RelationState.update") as mock_update,
    ):
        ctx.run(ctx.on.config_changed(), state_in)
        mock_update.assert_called_once()


def test_config_changed_leader_unit():
    ctx = testing.Context(ValkeyCharm)
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
    )
    with (
        patch("workload_k8s.ValkeyK8sWorkload.write_file"),
        patch("managers.config.ConfigManager.set_acl_file") as mock_set_acl_file,
        patch("common.client.ValkeyClient.load_acl") as mock_load_acl,
    ):
        state_out = ctx.run(ctx.on.config_changed(), state_in)
        mock_set_acl_file.assert_called_once()
        mock_load_acl.assert_called_once()
        secret_out = state_out.get_secret(label=f"{PEER_RELATION}.{APP_NAME}.app")
        assert (
            secret_out.latest_content.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
            == "secure-password"
        )


def test_config_changed_leader_unit_wrong_username():
    ctx = testing.Context(ValkeyCharm)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    password_secret = testing.Secret(
        tracked_content={"wrong-username": "secure-password"}, remote_grants=APP_NAME
    )
    state_in = testing.State(
        leader=True,
        relations={relation},
        containers={container},
        secrets={password_secret},
        config={INTERNAL_USERS_PASSWORD_CONFIG: password_secret.id},
    )
    with (
        patch("workload_k8s.ValkeyK8sWorkload.write_file"),
        patch("managers.config.ConfigManager.set_acl_file") as mock_set_acl_file,
    ):
        ctx.run(ctx.on.config_changed(), state_in)
        mock_set_acl_file.assert_not_called()


def test_change_password_secret_changed_non_leader_unit():
    ctx = testing.Context(ValkeyCharm)
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
    )
    with (
        patch("events.base_events.BaseEvents.update_admin_password") as mock_update_password,
    ):
        ctx.run(ctx.on.secret_changed(password_secret), state_in)
        mock_update_password.assert_not_called()


def test_change_password_secret_changed_leader_unit():
    ctx = testing.Context(ValkeyCharm)
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
    )
    with (
        patch("events.base_events.BaseEvents.update_admin_password") as mock_update_password,
    ):
        ctx.run(ctx.on.secret_changed(password_secret), state_in)
        mock_update_password.assert_called_once_with(password_secret.id)
