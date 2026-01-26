#!/usr/bin/env python3
# Copyright 2025 Canonical Limited
# See LICENSE file for licensing details.

"""Valkey base event handlers."""

import logging
import socket
from typing import TYPE_CHECKING

import ops

from common.exceptions import ValkeyClientError
from literals import INTERNAL_USER, INTERNAL_USER_PASSWORD_CONFIG, PEER_RELATION
from statuses import CharmStatuses, ClusterStatuses

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
        self.framework.observe(self.charm.on.config_changed, self._on_config_changed)
        self.framework.observe(self.charm.on.secret_changed, self._on_secret_changed)

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
                # TODO consider deferring and blocking the charm
                except (ops.ModelError, ops.SecretNotFoundError) as e:
                    logger.error(f"Could not access secret {admin_secret_id}: {e}")
                    raise
            else:
                password = self.charm.config_manager.generate_password()

            self.charm.state.cluster.update({"charmed_operator_password": password})
            self.charm.config_manager.set_acl_file()

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Handle the config_changed event."""
        self.charm.state.unit_server.update({"hostname": socket.gethostname()})

        if not self.charm.unit.is_leader():
            return

        if admin_secret_id := self.charm.config.get(INTERNAL_USER_PASSWORD_CONFIG):
            try:
                self.update_admin_password(str(admin_secret_id))
            except (ops.ModelError, ops.SecretNotFoundError):
                event.defer()
                return

    def _on_secret_changed(self, event: ops.SecretChangedEvent) -> None:
        """Handle the secret_changed event."""
        # TODO For a multi-node cluster the units should independently update their passwords.
        # If they fail the event should be deferred and retried.
        if not self.charm.unit.is_leader():
            return

        if admin_secret_id := self.charm.config.get(INTERNAL_USER_PASSWORD_CONFIG):
            if admin_secret_id == event.secret.id:
                try:
                    self.update_admin_password(str(admin_secret_id))
                except (ops.ModelError, ops.SecretNotFoundError):
                    event.defer()
                    return

    def update_admin_password(self, admin_secret_id: str) -> None:
        """Compare current admin password and update in valkey if required."""
        try:
            if new_password := self.charm.state.get_secret_from_id(admin_secret_id).get(
                INTERNAL_USER
            ):
                # only update admin credentials if the password has changed
                if new_password != self.charm.state.cluster.internal_user_credentials.get(
                    INTERNAL_USER
                ):
                    logger.debug(f"{INTERNAL_USER_PASSWORD_CONFIG} have changed.")
                    try:
                        self.charm.config_manager.set_acl_file(new_password)
                        self.charm.cluster_manager.load_acl_file()
                        self.charm.state.cluster.update(
                            {"charmed_operator_password": new_password}
                        )
                    except ValkeyClientError as e:
                        logger.error(e)
                        self.charm.status.set_running_status(
                            ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                            scope="app",
                            component_name=self.charm.cluster_manager.name,
                            statuses_state=self.charm.state.statuses,
                        )
                        return
            else:
                logger.error(f"Invalid username in secret {admin_secret_id}.")
                self.charm.status.set_running_status(
                    ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                    scope="app",
                    component_name=self.charm.cluster_manager.name,
                    statuses_state=self.charm.state.statuses,
                )
                return
        except (ops.ModelError, ops.SecretNotFoundError) as e:
            logger.error(e)
            self.charm.status.set_running_status(
                CharmStatuses.SECRET_ACCESS_ERROR.value,
                scope="app",
                component_name=self.charm.cluster_manager.name,
                statuses_state=self.charm.state.statuses,
            )
            raise

        self.charm.state.statuses.delete(
            ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
            scope="app",
            component=self.charm.cluster_manager.name,
        )
        self.charm.state.statuses.delete(
            CharmStatuses.SECRET_ACCESS_ERROR.value,
            scope="app",
            component=self.charm.cluster_manager.name,
        )
