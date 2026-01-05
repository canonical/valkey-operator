#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import ops
from common.literals import PEER_RELATION, STATUS_PEERS_RELATION
from common.statuses import CharmStatuses
from ops import testing

from .helpers import status_is

CHARM_USER = "valkey"
CONTAINER = "valkey"
SERVICE_VALKEY = "valkey"
SERVICE_METRIC_EXPORTER = "metric_exporter"


class TestBaseEvents():
    """Wrapper class around common tests for events/base_events.py"""
    def __init__(self, charm: ops.CharmBase):
        self.charm = charm

    def run_all_tests(self):
        self.test_update_status_leader_unit()
        self.test_update_status_non_leader_unit()

    def test_update_status_leader_unit(self):
        ctx = testing.Context(self.charm)
        relation = testing.PeerRelation(
            id=1,
            endpoint=PEER_RELATION,
            local_unit_data={"started": "True"},
        )
        status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

        # happy path
        container = testing.Container(name=CONTAINER, can_connect=True)
        state_in = testing.State(
            leader=True,
            relations={relation, status_peer_relation},
            containers={container},
        )

        state_out = ctx.run(ctx.on.update_status(), state_in)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_update_status_non_leader_unit(self):
        ctx = testing.Context(self.charm)
        relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
        status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

        container = testing.Container(name=CONTAINER, can_connect=True)
        state_in = testing.State(
            leader=False,
            relations={relation, status_peer_relation},
            containers={container},
        )
        state_out = ctx.run(ctx.on.update_status(), state_in)
        assert status_is(state_out, CharmStatuses.SCALING_NOT_IMPLEMENTED.value)