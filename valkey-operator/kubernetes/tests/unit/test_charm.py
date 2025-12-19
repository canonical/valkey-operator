#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import ops
from ops import testing

from charm import ValkeyK8sCharm
from common.literals import PEER_RELATION, STATUS_PEERS_RELATION
from common.statuses import CharmStatuses

from .helpers import status_is

CHARM_USER = "valkey"
CONTAINER = "valkey"
SERVICE_VALKEY = "valkey"
SERVICE_METRIC_EXPORTER = "metric_exporter"


def test_pebble_ready_leader_unit():
    ctx = testing.Context(ValkeyK8sCharm)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
    )

    expected_plan = {
        "services": {
            SERVICE_VALKEY: {
                "override": "replace",
                "summary": "Valkey service",
                "command": "valkey-server",
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
        == ops.pebble.ServiceStatus.ACTIVE
    )
    assert (
            state_out.get_container(container.name).service_statuses[SERVICE_METRIC_EXPORTER]
            == ops.pebble.ServiceStatus.ACTIVE
    )
    assert state_out.unit_status == ops.ActiveStatus()


def test_pebble_ready_non_leader_unit():
    ctx = testing.Context(ValkeyK8sCharm)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={relation, status_peer_relation},
        containers={container},
    )

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)
    assert not state_out.get_container(container.name).service_statuses.get(SERVICE_VALKEY)
    assert not state_out.get_container(container.name).service_statuses.get(SERVICE_METRIC_EXPORTER)
    assert status_is(state_out, CharmStatuses.SCALING_NOT_IMPLEMENTED.value)
