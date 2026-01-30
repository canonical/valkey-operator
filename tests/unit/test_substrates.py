#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import patch

from ops import testing
from pytest import raises

from src.charm import ValkeyCharm
from src.literals import PEER_RELATION, STATUS_PEERS_RELATION


def test_install_failure(cloud_spec_vm):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)

    state_in = testing.State(
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec_vm),
        leader=True,
        relations={relation, status_peer_relation},
    )

    with (
        patch("workload_vm.ValkeyVmWorkload.install", side_effect=RuntimeError()),
        patch("workload_vm.ValkeyVmWorkload.exec"),
    ):
        with raises(testing.errors.UncaughtCharmError) as e:
            ctx.run(ctx.on.install(), state_in)
        assert isinstance(e.value.__cause__, RuntimeError)
