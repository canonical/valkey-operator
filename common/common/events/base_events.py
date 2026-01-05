#!/usr/bin/env python3
# Copyright 2025 Canonical Limited
# See LICENSE file for licensing details.

"""Valkey base event handlers."""

import logging

import ops

from common.literals import PEER_RELATION

logger = logging.getLogger(__name__)


class BaseEvents(ops.Object):
    """Handle all base events."""

    def __init__(self, charm: ops.CharmBase):
        super().__init__(charm, key="base_events")
        self.charm = charm

        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_joined, self._on_peer_relation_joined
        )
        self.framework.observe(self.charm.on.update_status, self._on_update_status)

    def _on_peer_relation_joined(self, event: ops.RelationJoinedEvent) -> None:
        """Handle event received by all units when a new unit joins the cluster relation."""
        if self.charm.unit.is_leader():
            logger.info("Unit %s has joined the relation", event.unit.name)

    def _on_update_status(self, event: ops.UpdateStatusEvent) -> None:
        """Handle the update-status event."""
        if not self.charm.state.unit_server.is_started:
            logger.warning("Service not started")
