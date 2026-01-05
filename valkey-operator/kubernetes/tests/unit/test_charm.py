#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import ops
from common.literals import PEER_RELATION, STATUS_PEERS_RELATION
from common.statuses import CharmStatuses
from common.tests.unit.test_base_events import TestBaseEvents
from ops import testing

from charm import ValkeyK8sCharm

from .helpers import status_is

CHARM_USER = "valkey"
CONTAINER = "valkey"
SERVICE_VALKEY = "valkey"
SERVICE_METRIC_EXPORTER = "metric_exporter"


def test_pebble_ready_leader_unit():
    ctx = testing.Context(ValkeyK8sCharm)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    # happy path
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
        == ops.pebble.ServiceStatus.ACTIVE
    )
    assert (
        state_out.get_container(container.name).service_statuses[SERVICE_METRIC_EXPORTER]
        == ops.pebble.ServiceStatus.ACTIVE
    )
    assert state_out.unit_status == ops.ActiveStatus()
    assert status_is(state_out, CharmStatuses.SCALING_NOT_IMPLEMENTED.value, is_app=True)

    # container not ready
    container = testing.Container(name=CONTAINER, can_connect=False)
    state_in = testing.State(
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
    )

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)
    assert status_is(state_out, CharmStatuses.SERVICE_NOT_STARTED.value)
    assert status_is(state_out, CharmStatuses.SERVICE_NOT_STARTED.value, is_app=True)


def test_pebble_ready_non_leader_unit():
    ctx = testing.Context(ValkeyK8sCharm)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    # happy path
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
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
        leader=True,
        relations={relation, status_peer_relation},
        containers={container},
    )

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)
    assert status_is(state_out, CharmStatuses.SERVICE_NOT_STARTED.value)


def test_base_events():
    base_events_test = TestBaseEvents(ValkeyK8sCharm)
    base_events_test.run_all_tests()
