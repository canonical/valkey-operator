#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from ops import testing

from src.charm import ValkeyCharm
from src.literals import PEER_RELATION, PEER_TLS_RELATION_NAME, STATUS_PEERS_RELATION
from src.statuses import TLSStatuses

from .helpers import status_is

CONTAINER = "valkey"


def test_peer_tls_relation_created(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"started": "True"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    peer_tls_relation = testing.Relation(id=3, endpoint=PEER_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, peer_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    state_out = ctx.run(ctx.on.relation_created(relation=peer_tls_relation), state_in)
    assert status_is(state_out, TLSStatuses.ENABLING_PEER_TLS.value)


def test_peer_tls_relation_broken(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"started": "True"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    peer_tls_relation = testing.Relation(id=3, endpoint=PEER_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, peer_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    state_out = ctx.run(ctx.on.relation_broken(relation=peer_tls_relation), state_in)
    assert status_is(state_out, TLSStatuses.DISABLING_PEER_TLS.value)
