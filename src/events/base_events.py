#!/usr/bin/env python3
# Copyright 2025 Canonical Limited
# See LICENSE file for licensing details.

"""Valkey base event handlers."""

import logging
from typing import TYPE_CHECKING

import ops

from literals import INTERNAL_USER, INTERNAL_USER_PASSWORD_CONFIG, PEER_RELATION

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class BaseEvents(ops.Object):
    """Handle all base events."""

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="base_events")
        self.charm = charm

        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_joined, self._on_peer_relation_joined
        )
        self.framework.observe(self.charm.on.update_status, self._on_update_status)
        self.framework.observe(self.charm.on.leader_elected, self._on_leader_elected)

    def _on_peer_relation_joined(self, event: ops.RelationJoinedEvent) -> None:
        """Handle event received by all units when a new unit joins the cluster relation."""
        if self.charm.unit.is_leader():
            logger.info("Unit %s has joined the relation", event.unit.name)

    def _on_update_status(self, event: ops.UpdateStatusEvent) -> None:
        """Handle the update-status event."""
        if not self.charm.state.unit_server.is_started:
            logger.warning("Service not started")

    def _on_leader_elected(self, event: ops.LeaderElectedEvent) -> None:
        """Handle the leader-elected event."""
        if not self.charm.state.peer_relation:
            event.defer()
            return

        if self.charm.unit.is_leader() and not self.charm.state.cluster.internal_user_credentials:
            if admin_secret_id := self.charm.config.get(INTERNAL_USER_PASSWORD_CONFIG):
                try:
                    password = self.charm.state.get_secret_from_id(str(admin_secret_id)).get(
                        INTERNAL_USER
                    )
                except (ops.ModelError, ops.SecretNotFoundError) as e:
                    logger.error(f"Could not access secret {admin_secret_id}: {e}")
                    raise
            else:
                password = self.charm.config_manager.generate_password()

            self.charm.state.cluster.update({"charmed_operator_password": password})
            self.charm.config_manager.set_acl_file()
