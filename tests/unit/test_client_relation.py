#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from ops import testing

from src.charm import ValkeyCharm
from src.common.exceptions import ValkeyCannotGetPrimaryIPError, ValkeyWorkloadCommandError
from src.literals import (
    CLIENT_PORT,
    CLIENTS_USERS_SECRET_LABEL_SUFFIX,
    EXTERNAL_CLIENTS_RELATION,
    PEER_RELATION,
    SENTINEL_PORT,
    STATUS_PEERS_RELATION,
)
from src.statuses import ExternalClientsStatuses

from .helpers import status_is

CONTAINER = "valkey"

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


def test_add_new_client_user(cloud_spec):
    primary_endpoint = "valkey-0.valkey-endpoints"
    replica_endpoint = "valkey-1.valkey-endpoints"
    valkey_version = "9.0.1"
    key_prefix = "test:*"
    request_id = "0cbbc9781f189ea5"
    salt = "mWpK32IQW4bsu65t"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "hostname": primary_endpoint},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_relation = testing.Relation(
        id=3,
        endpoint=EXTERNAL_CLIENTS_RELATION,
        remote_app_data={
            "version": "v1",
            "requests": f'[{{"resource": "{key_prefix}", "request-id": "{request_id}", "salt": "{salt}"}}]',
        },
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value=primary_endpoint),
        patch(
            "common.client.SentinelClient.replicas_primary",
            return_value=[{"ip": replica_endpoint}],
        ),
        # patch("charmlibs.pathops.ContainerPath.read_text", return_value="my_ca"),
        patch(
            "common.client.ValkeyClient.info_server",
            return_value={"valkey_version": valkey_version},
        ),
        patch("managers.config.ConfigManager.set_acl_file") as set_acl_file,
        patch("common.client.ValkeyClient.acl_load") as load_acl,
    ):
        state_out = ctx.run(ctx.on.relation_changed(relation=client_relation), state_in)
        set_acl_file.assert_called_once()
        load_acl.assert_called_once()
        relation = state_out.get_relation(client_relation.id)
        response = json.loads(relation.local_app_data["requests"])[0]
        secret_user_id = response["secret-user"]
        secret_user = state_out.get_secret(id=secret_user_id)
        secret_tls_id = response["secret-tls"]
        secret_tls = state_out.get_secret(id=secret_tls_id)

        assert response["resource"] == key_prefix
        assert response["request-id"] == request_id
        assert response["salt"] == salt
        assert response["endpoints"] == f"{primary_endpoint}:{CLIENT_PORT}"
        assert response["read-only-endpoints"] == f"{replica_endpoint}:{CLIENT_PORT}"
        assert response["sentinel-endpoints"] == f"{primary_endpoint}:{SENTINEL_PORT}"
        assert response["version"] == valkey_version
        assert response["mode"] == "sentinel"
        assert (
            secret_user.latest_content.get("username")
            == f"relation-{client_relation.id}-{request_id}"
        )
        assert secret_user.latest_content.get("password")
        assert secret_tls.latest_content.get("tls") == "false"


def test_add_new_client_user_v0(cloud_spec):
    primary_endpoint = "valkey-0.valkey-endpoints"
    replica_endpoint = "valkey-1.valkey-endpoints"
    valkey_version = "9.0.1"
    key_prefix = "test:*"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "hostname": primary_endpoint},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_relation = testing.Relation(
        id=3,
        endpoint=EXTERNAL_CLIENTS_RELATION,
        remote_app_data={"database": f"{key_prefix}"},
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value=primary_endpoint),
        patch(
            "common.client.SentinelClient.replicas_primary",
            return_value=[{"ip": replica_endpoint}],
        ),
        patch(
            "common.client.ValkeyClient.info_server",
            return_value={"valkey_version": valkey_version},
        ),
        patch("managers.config.ConfigManager.set_acl_file") as set_acl_file,
        patch("common.client.ValkeyClient.acl_load") as load_acl,
    ):
        state_out = ctx.run(ctx.on.relation_changed(relation=client_relation), state_in)
        set_acl_file.assert_called_once()
        load_acl.assert_called_once()
        relation = state_out.get_relation(client_relation.id)
        response = relation.local_app_data
        secret_user_id = response["secret-user"]
        secret_user = state_out.get_secret(id=secret_user_id)
        secret_tls_id = response["secret-tls"]
        secret_tls = state_out.get_secret(id=secret_tls_id)

        assert response["database"] == key_prefix
        assert response["endpoints"] == f"{primary_endpoint}:{CLIENT_PORT}"
        assert response["read-only-endpoints"] == f"{replica_endpoint}:{CLIENT_PORT}"
        assert response["sentinel_endpoints"] == f"{primary_endpoint}:{SENTINEL_PORT}"
        assert response["version"] == valkey_version
        assert response["mode"] == "sentinel"
        assert secret_user.latest_content.get("username") == f"relation-{client_relation.id}"
        assert secret_user.latest_content.get("password")
        assert secret_tls.latest_content.get("tls") == "false"


def test_client_request_failed(cloud_spec):
    primary_endpoint = "valkey-0.valkey-endpoints"
    key_prefix = "test:*"
    request_id = "0cbbc9781f189ea5"
    salt = "mWpK32IQW4bsu65t"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "hostname": primary_endpoint},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_relation = testing.Relation(
        id=3,
        endpoint=EXTERNAL_CLIENTS_RELATION,
        remote_app_data={
            "version": "v1",
            "requests": f'[{{"resource": "{key_prefix}", "request-id": "{request_id}", "salt": "{salt}"}}]',
        },
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch(
            "managers.sentinel.SentinelManager.get_primary_ip",
            side_effect=ValkeyCannotGetPrimaryIPError("error"),
        ),
    ):
        state_out = ctx.run(ctx.on.relation_changed(relation=client_relation), state_in)
        assert status_is(
            state_out, ExternalClientsStatuses.RESOURCE_REQUEST_FAILED.value, is_app=True
        )


def test_client_request_acl_load_failed(cloud_spec):
    primary_endpoint = "valkey-0.valkey-endpoints"
    key_prefix = "test:*"
    request_id = "0cbbc9781f189ea5"
    salt = "mWpK32IQW4bsu65t"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "hostname": primary_endpoint},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_relation = testing.Relation(
        id=3,
        endpoint=EXTERNAL_CLIENTS_RELATION,
        remote_app_data={
            "version": "v1",
            "requests": f'[{{"resource": "{key_prefix}", "request-id": "{request_id}", "salt": "{salt}"}}]',
        },
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.sentinel.SentinelManager.get_primary_ip"),
        patch("common.client.SentinelClient.replicas_primary"),
        patch("common.client.ValkeyClient.info_server"),
        patch("managers.config.ConfigManager.set_acl_file"),
        patch(
            "common.client.ValkeyClient.exec_cli_command",
            side_effect=ValkeyWorkloadCommandError("Failed to reload ACLs"),
        ),
    ):
        state_out = ctx.run(ctx.on.relation_changed(relation=client_relation), state_in)
        assert status_is(
            state_out, ExternalClientsStatuses.RESOURCE_REQUEST_FAILED.value, is_app=True
        )


def test_add_new_client_user_non_leader(cloud_spec):
    primary_endpoint = "valkey-0.valkey-endpoints"
    key_prefix = "test:*"
    request_id = "0cbbc9781f189ea5"
    salt = "mWpK32IQW4bsu65t"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "hostname": primary_endpoint},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_relation = testing.Relation(
        id=3,
        endpoint=EXTERNAL_CLIENTS_RELATION,
        remote_app_data={
            "version": "v1",
            "requests": f'[{{"resource": "{key_prefix}", "request-id": "{request_id}", "salt": "{salt}"}}]',
        },
    )
    managed_users_secret = testing.Secret(
        tracked_content={
            "external-client-users": """ \
            {"relation-3-0cbbc9781f189ea5": \
            {"resource": "test:*", "password": "mypassword"}} \
            """
        },
        owner="app",
        label=f"{PEER_RELATION}.{APP_NAME}.app.{CLIENTS_USERS_SECRET_LABEL_SUFFIX}",
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, client_relation},
        secrets={managed_users_secret},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.config.ConfigManager.set_acl_file") as set_acl_file,
        patch("common.client.ValkeyClient.acl_load") as load_acl,
    ):
        ctx.run(ctx.on.relation_changed(relation=peer_relation, remote_unit=1), state_in)
        set_acl_file.assert_called_once()
        load_acl.assert_called_once()


def test_remove_client_user(cloud_spec):
    primary_endpoint = "valkey-0.valkey-endpoints"
    key_prefix = "test:*"
    request_id = "0cbbc9781f189ea5"
    salt = "mWpK32IQW4bsu65t"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "hostname": primary_endpoint},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_relation = testing.Relation(
        id=3,
        endpoint=EXTERNAL_CLIENTS_RELATION,
        remote_app_data={
            "version": "v1",
            "requests": f'[{{"resource": "{key_prefix}", "request-id": "{request_id}", "salt": "{salt}"}}]',
        },
    )
    managed_users_secret = testing.Secret(
        tracked_content={
            "external-client-users": """ \
                {"relation-3-0cbbc9781f189ea5": \
                {"resource": "test:*", "password": "mypassword"}, \
                "relation-4-08154711": \
                {"resource": "another_keyspace:*", "password": "anotherpassword"}} \
                """
        },
        owner="app",
        label=f"{PEER_RELATION}.{APP_NAME}.app.{CLIENTS_USERS_SECRET_LABEL_SUFFIX}",
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_relation},
        secrets={managed_users_secret},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.config.ConfigManager.set_acl_file") as set_acl_file,
        patch("common.client.ValkeyClient.acl_load") as load_acl,
    ):
        state_out = ctx.run(ctx.on.relation_broken(relation=client_relation), state_in)
        set_acl_file.assert_called_once()
        load_acl.assert_called_once()

        managed_users_secret = state_out.get_secret(
            label=f"{PEER_RELATION}.{APP_NAME}.app.{CLIENTS_USERS_SECRET_LABEL_SUFFIX}"
        )
        managed_users = json.loads(
            managed_users_secret.latest_content.get("external-client-users")
        )
        assert not managed_users.get(f"relation-{client_relation.id}-{request_id}")
        assert managed_users.get("relation-4-08154711")


def test_relation_broken_non_leader(cloud_spec):
    primary_endpoint = "valkey-0.valkey-endpoints"
    key_prefix = "test:*"
    request_id = "0cbbc9781f189ea5"
    salt = "mWpK32IQW4bsu65t"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "hostname": primary_endpoint},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_relation = testing.Relation(
        id=3,
        endpoint=EXTERNAL_CLIENTS_RELATION,
        remote_app_data={
            "version": "v1",
            "requests": f'[{{"resource": "{key_prefix}", "request-id": "{request_id}", "salt": "{salt}"}}]',
        },
    )
    managed_users_secret = testing.Secret(
        tracked_content={
            # the managed user was already removed by the leader
            "external-client-users": """ \
                {"relation-4-08154711": \
                {"resource": "another_keyspace:*", "password": "anotherpassword"}} \
                """
        },
        owner="app",
        label=f"{PEER_RELATION}.{APP_NAME}.app.{CLIENTS_USERS_SECRET_LABEL_SUFFIX}",
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, client_relation},
        secrets={managed_users_secret},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.config.ConfigManager.set_acl_file") as set_acl_file,
        patch(
            "managers.external_clients.ExternalClientsManager.remove_managed_users"
        ) as remove_user,
        patch("common.client.ValkeyClient.acl_load") as load_acl,
    ):
        ctx.run(ctx.on.relation_broken(relation=client_relation), state_in)
        remove_user.assert_not_called()
        set_acl_file.assert_called_once()
        load_acl.assert_called_once()
