#!/usr/bin/env python3
# Copyright 2025 Canonical Limited
# See LICENSE file for licensing details.

"""Valkey base event handlers."""

import logging
import socket
from typing import TYPE_CHECKING

import ops

from common.exceptions import ValkeyACLLoadError
from literals import (
    INTERNAL_USERS_PASSWORD_CONFIG,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    PEER_RELATION,
    CharmUsers,
)
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

        if not self.charm.unit.is_leader():
            return

        if self.charm.state.cluster.internal_users_credentials:
            logger.debug("Internal user credentials already set")
            return

        passwords = {}
        user_specified_passwords = {}
        if admin_secret_id := self.charm.config.get(INTERNAL_USERS_PASSWORD_CONFIG):
            try:
                user_specified_passwords = self.charm.state.get_secret_from_id(
                    str(admin_secret_id)
                )
            except (ops.ModelError, ops.SecretNotFoundError) as e:
                logger.error(f"Could not access secret {admin_secret_id}: {e}")
                raise

        # generate passwords for all internal users if not specified in the user secret
        for user in CharmUsers:
            passwords[user.value] = user_specified_passwords.get(
                user.value, self.charm.config_manager.generate_password()
            )

        self.charm.state.cluster.update(
            {
                f"{user.value.replace('-', '_')}_password": passwords[user.value]
                for user in CharmUsers
            }
        )
        self.charm.config_manager.set_acl_file()

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Handle the config_changed event."""
        self.charm.state.unit_server.update({"hostname": socket.gethostname()})

        if not self.charm.unit.is_leader():
            return

        if admin_secret_id := self.charm.config.get(INTERNAL_USERS_PASSWORD_CONFIG):
            try:
                self._update_internal_users_password(str(admin_secret_id))
            except (ops.ModelError, ops.SecretNotFoundError, ValkeyACLLoadError):
                event.defer()
                return

    def _on_secret_changed(self, event: ops.SecretChangedEvent) -> None:
        """Handle the secret_changed event."""
        if not (admin_secret_id := self.charm.config.get(INTERNAL_USERS_PASSWORD_CONFIG)):
            return

        if self.charm.unit.is_leader():
            if admin_secret_id == event.secret.id:
                try:
                    self._update_internal_users_password(str(admin_secret_id))
                except (ops.ModelError, ops.SecretNotFoundError, ValkeyACLLoadError):
                    event.defer()
                    return
            return

        # from here, code is only relevant for non-leader units
        if event.secret.label and event.secret.label.endswith(INTERNAL_USERS_SECRET_LABEL_SUFFIX):
            # leader unit processed the secret change from user, non-leader units can replicate
            try:
                self.charm.config_manager.set_acl_file()
                self.charm.cluster_manager.reload_acl_file()
            except ValkeyACLLoadError as e:
                logger.error(e)
                self.charm.status.set_running_status(
                    ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                    scope="unit",
                    component_name=self.charm.cluster_manager.name,
                    statuses_state=self.charm.state.statuses,
                )
                event.defer()
                return
            self.charm.state.statuses.delete(
                ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                scope="unit",
                component=self.charm.cluster_manager.name,
            )

    def _update_internal_users_password(self, secret_id: str) -> None:
        """Update internal users' passwords in charm/valkey if they have changed.

        Args:
            secret_id (str): The id of the secret containing the internal users' passwords.
        """
        try:
            secret_content = self.charm.state.get_secret_from_id(secret_id)
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
            CharmStatuses.SECRET_ACCESS_ERROR.value,
            scope="app",
            component=self.charm.cluster_manager.name,
        )

        if any(key not in CharmUsers for key in secret_content.keys()):
            logger.error(f"Invalid username in secret {secret_id}.")
            self.charm.status.set_running_status(
                ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                scope="app",
                component_name=self.charm.cluster_manager.name,
                statuses_state=self.charm.state.statuses,
            )
            # do not raise here, we don't want to run again if data is wrong
            return

        # merge the credentials, replacing those which have been updated
        new_passwords = self.charm.state.cluster.internal_users_credentials | secret_content
        if new_passwords != self.charm.state.cluster.internal_users_credentials:
            logger.info("Password(s) for internal users have changed")
            try:
                self.charm.config_manager.set_acl_file(passwords=new_passwords)
                self.charm.cluster_manager.reload_acl_file()
                self.charm.state.cluster.update(
                    {
                        f"{user.value.replace('-', '_')}_password": new_passwords[user.value]
                        for user in CharmUsers
                    }
                )
            except (ValkeyACLLoadError, ValueError) as e:
                logger.error(e)
                self.charm.status.set_running_status(
                    ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                    scope="unit",
                    component_name=self.charm.cluster_manager.name,
                    statuses_state=self.charm.state.statuses,
                )
                raise e

        self.charm.state.statuses.delete(
            ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )
        self.charm.state.statuses.delete(
            ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
            scope="app",
            component=self.charm.cluster_manager.name,
        )
