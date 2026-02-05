#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from ops import testing

from src.charm import ValkeyCharm
from src.literals import PEER_TLS_RELATION_NAME

CONTAINER = "valkey"


def test_peer_tls_relation_created(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    tls_relation = testing.PeerRelation(id=1, endpoint=PEER_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    ctx.run(ctx.on.relation_created(relation=tls_relation), state_in)
    assert True
