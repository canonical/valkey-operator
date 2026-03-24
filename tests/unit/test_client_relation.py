#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from ops import testing

from src.charm import ValkeyCharm
from src.literals import (
    CLIENT_PORT,
    EXTERNAL_CLIENTS_RELATION,
    PEER_RELATION,
    SENTINEL_PORT,
    STATUS_PEERS_RELATION,
)

CONTAINER = "valkey"

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


def test_add_new_client_user(cloud_spec):
    primary_endpoint = "valkey-0.valkey-endpoints"
    replica_endpoint = "valkey-1.valkey-endpoints"
    valkey_version = "9.0.1"

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
            "requests": [
                {
                    {
                        "resource": "test:*",
                        "request-id": "0cbbc9781f189ea5",
                        "salt": "mWpK32IQW4bsu65t",
                    }
                }
            ],
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
        assert response["endpoints"] == f"{primary_endpoint}:{CLIENT_PORT}"
        assert response["read-only-endpoints"] == f"{replica_endpoint}:{CLIENT_PORT}"
        assert response["sentinel-endpoints"] == f"{primary_endpoint}:{SENTINEL_PORT}"
        assert response["version"] == valkey_version
        assert response["mode"] == "sentinel"
