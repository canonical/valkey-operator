#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Objects representing the cluster state of Valkey."""

import logging

import ops
from common.literals import PEER_RELATION, STATUS_PEERS_RELATION
from data_platform_helpers.advanced_statuses.protocol import StatusesState, StatusesStateProtocol

logger = logging.getLogger(__name__)


class ClusterState(ops.Object, StatusesStateProtocol):
    """Global state object for the etcd cluster."""

    def __init__(self, charm: ops.CharmBase):
        super().__init__(parent=charm, key="charm_state")
        self.charm = charm
        self.statuses_relation_name = STATUS_PEERS_RELATION
        self.statuses = StatusesState(self, self.statuses_relation_name)
        self.config = charm.config
