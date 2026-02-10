#!/usr/bin/env python3
# Copyright 2025 Canonical Limited
# See LICENSE file for licensing details.

"""Valkey base event handlers."""

import logging
import socket
from typing import TYPE_CHECKING

import ops

from common.exceptions import ValkeyACLLoadError, ValkeyConfigSetError, ValkeyWorkloadCommandError
from literals import (
    CLIENT_PORT,
    INTERNAL_USERS_PASSWORD_CONFIG,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    PEER_RELATION,
    CharmUsers,
    Substrate,
)
from statuses import CharmStatuses, ClusterStatuses, ValkeyServiceStatuses

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class UnitFullyStarted(ops.EventBase):
    """Event that signals that the unit's has fully started.

    This event will be deferred until:
        The Sentinel service is running and was discovered by other units.
        The Valkey service is running and the replica has finished syncing data.
    """


class BaseEvents(ops.Object):
    """Handle all base events."""

    unit_fully_started = ops.EventSource(UnitFullyStarted)

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="base_events")
        self.charm = charm

        self.framework.observe(self.charm.on.install, self._on_install)
        self.framework.observe(self.charm.on.start, self._on_start)
        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_joined, self._on_peer_relation_joined
        )
        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_changed, self._on_peer_relation_changed
        )
        self.framework.observe(self.charm.on.update_status, self._on_update_status)
        self.framework.observe(self.charm.on.leader_elected, self._on_leader_elected)
        self.framework.observe(self.charm.on.config_changed, self._on_config_changed)
        self.framework.observe(self.charm.on.secret_changed, self._on_secret_changed)
        self.framework.observe(self.unit_fully_started, self._on_unit_fully_started)

    def _on_install(self, event: ops.InstallEvent) -> None:
        """Handle install event."""
        if self.charm.substrate == Substrate.K8S:
            logger.debug("No installation required.")
            return

        try:
            self.charm.workload.install()
        except RuntimeError:
            raise RuntimeError("Failed to install the Valkey snap")

    def _on_start(self, event: ops.StartEvent) -> None:
        """Handle the `pebble-ready` event."""
        if not self.charm.workload.can_connect:
            logger.warning("Workload not ready yet")
            event.defer()
            return

        if not self.charm.unit.is_leader():
            if (
                not self.charm.state.cluster.internal_users_credentials
                or not self.charm.cluster_manager.number_units_started
            ):
                logger.info(
                    "Non-leader unit waiting for leader to set primary and internal user credentials"
                )
                self.charm.status.set_running_status(
                    ClusterStatuses.WAITING_FOR_PRIMARY_START.value,
                    scope="unit",
                    component_name=self.charm.cluster_manager.name,
                    statuses_state=self.charm.state.statuses,
                )
                event.defer()
                return

            self.charm.state.statuses.delete(
                ClusterStatuses.WAITING_FOR_PRIMARY_START.value,
                scope="unit",
                component=self.charm.cluster_manager.name,
            )
            if self.charm.state.cluster.model.starting_member != self.charm.unit.name:
                logger.info("Non-leader unit waiting for leader to choose it as starting member")
                self.charm.status.set_running_status(
                    CharmStatuses.WAITING_TO_START.value,
                    scope="unit",
                    component_name=self.charm.cluster_manager.name,
                    statuses_state=self.charm.state.statuses,
                )
                event.defer()
                return
            self.charm.state.statuses.delete(
                CharmStatuses.WAITING_TO_START.value,
                scope="unit",
                component=self.charm.cluster_manager.name,
            )

        if not (
            primary_ip := (
                self.charm.workload.get_private_ip()
                if self.charm.unit.is_leader()
                else self.charm.cluster_manager.get_primary_ip()
            )
        ):
            logger.error("Primary IP not found. Deferring start event.")
            event.defer()
            return

        try:
            self.charm.config_manager.update_local_valkey_admin()
            self.charm.config_manager.set_config_properties(primary_ip=primary_ip)
            self.charm.config_manager.set_acl_file()
            self.charm.config_manager.set_sentinel_config_properties(primary_ip=primary_ip)
            self.charm.config_manager.set_sentinel_acl_file()
        except (ValkeyWorkloadCommandError, ValueError):
            logger.error("Failed to set configuration")
            self.charm.status.set_running_status(
                CharmStatuses.CONFIGURATION_ERROR.value,
                scope="unit",
                component_name=self.charm.cluster_manager.name,
                statuses_state=self.charm.state.statuses,
            )
            event.defer()
            return
        self.charm.state.statuses.delete(
            CharmStatuses.CONFIGURATION_ERROR.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )
        self.charm.status.set_running_status(
            ValkeyServiceStatuses.SERVICE_STARTING.value,
            scope="unit",
            component_name=self.charm.cluster_manager.name,
            statuses_state=self.charm.state.statuses,
        )

        self.charm.workload.start()
        if self.charm.workload.alive():
            logger.info("Workload started successfully. Opening client port")
            self.charm.unit.open_port("tcp", CLIENT_PORT)
            self.charm.state.statuses.delete(
                ValkeyServiceStatuses.SERVICE_STARTING.value,
                scope="unit",
                component=self.charm.cluster_manager.name,
            )
        else:
            logger.error("Workload failed to start.")
            self.charm.status.set_running_status(
                ValkeyServiceStatuses.SERVICE_NOT_RUNNING.value,
                scope="unit",
                component_name=self.charm.cluster_manager.name,
                statuses_state=self.charm.state.statuses,
            )

        self.charm.state.statuses.delete(
            ValkeyServiceStatuses.SERVICE_NOT_RUNNING.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )
        if self.charm.unit.is_leader():
            logger.info("Services started")
            self.charm.state.unit_server.update({"started": True})
            return

        self.unit_fully_started.emit()

    # TODO check how to trigger if deferred without update status event
    def _on_unit_fully_started(self, event: UnitFullyStarted) -> None:
        """Handle the unit-fully-started event."""
        self.charm.status.set_running_status(
            ClusterStatuses.WAITING_FOR_SENTINEL_DISCOVERY.value,
            scope="unit",
            component_name=self.charm.cluster_manager.name,
            statuses_state=self.charm.state.statuses,
        )
        self.charm.status.set_running_status(
            ClusterStatuses.WAITING_FOR_REPLICA_SYNC.value,
            scope="unit",
            component_name=self.charm.cluster_manager.name,
            statuses_state=self.charm.state.statuses,
        )

        if not self.charm.cluster_manager.is_sentinel_discovered():
            logger.info("Sentinel service not yet discovered by other units. Deferring event.")
            event.defer()
            return

        self.charm.state.statuses.delete(
            ClusterStatuses.WAITING_FOR_SENTINEL_DISCOVERY.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )

        if not self.charm.cluster_manager.is_replica_synced():
            logger.info("Replica not yet synced. Deferring event.")
            event.defer()
            return

        self.charm.state.statuses.delete(
            ClusterStatuses.WAITING_FOR_REPLICA_SYNC.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )

        logger.info("Services started")
        self.charm.state.unit_server.update({"started": True})

    def _on_peer_relation_joined(self, event: ops.RelationJoinedEvent) -> None:
        """Handle event received by all units when a new unit joins the cluster relation."""
        if not self.charm.unit.is_leader() or not event.unit:
            return

        logger.debug("Peer relation joined by %s", event.unit.name)

        if not self.charm.state.unit_server.is_started:
            logger.info("Primary member has not started yet. Deferring event.")
            event.defer()
            return

        if self.charm.state.cluster.model.starting_member:
            logger.debug(
                "%s is already starting. Deferring relation joined event for %s",
                self.charm.state.cluster.model.starting_member,
                event.unit.name,
            )
            event.defer()
            return
        self.charm.state.cluster.update({"starting_member": event.unit.name})

    def _on_peer_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle event received by all units when a unit's relation data changes."""
        logger.debug(
            "Starting member is currently %s", self.charm.state.cluster.model.starting_member
        )
        starting_unit = next(
            (
                unit
                for unit in self.charm.state.servers
                if unit.unit_name == self.charm.state.cluster.model.starting_member
            ),
            None,
        )
        logger.debug(
            "Starting unit has started: %s",
            starting_unit.is_started if starting_unit else "No starting unit",
        )
        if (
            self.charm.state.cluster.model.starting_member
            and starting_unit
            and starting_unit.is_started
        ):
            logger.debug(
                "Starting member %s has started. Clearing starting member field.",
                self.charm.state.cluster.model.starting_member,
            )
            self.charm.state.cluster.update({"starting_member": ""})

    def _on_update_status(self, event: ops.UpdateStatusEvent) -> None:
        """Handle the update-status event."""
        if not self.charm.state.unit_server.is_started:
            logger.warning("Service not started")

    def _on_leader_elected(self, event: ops.LeaderElectedEvent) -> None:
        """Handle the leader-elected event."""
        if not self.charm.state.peer_relation:
            event.defer()
            return

        self.charm.state.unit_server.update(
            {
                "hostname": socket.gethostname(),
                "private_ip": self.charm.workload.get_private_ip(),
            }
        )

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
        # update local unit admin password
        self.charm.config_manager.update_local_valkey_admin()
        try:
            self.charm.config_manager.set_acl_file()
        except ValkeyWorkloadCommandError:
            logger.error("Failed to write acl file")
            raise

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Handle the config_changed event."""
        self.charm.state.unit_server.update(
            {
                "hostname": socket.gethostname(),
                "private_ip": self.charm.workload.get_private_ip(),
            }
        )

        if not self.charm.unit.is_leader():
            return

        if admin_secret_id := self.charm.config.get(INTERNAL_USERS_PASSWORD_CONFIG):
            try:
                self._update_internal_users_password(str(admin_secret_id))
            except (
                ops.ModelError,
                ops.SecretNotFoundError,
                ValkeyACLLoadError,
                ValkeyWorkloadCommandError,
            ):
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
                except (
                    ops.ModelError,
                    ops.SecretNotFoundError,
                    ValkeyACLLoadError,
                    ValkeyWorkloadCommandError,
                ):
                    event.defer()
                    return
            return

        # from here, code is only relevant for non-leader units
        if event.secret.label and event.secret.label.endswith(INTERNAL_USERS_SECRET_LABEL_SUFFIX):
            # leader unit processed the secret change from user, non-leader units can replicate
            try:
                self.charm.config_manager.set_acl_file()
                self.charm.cluster_manager.reload_acl_file()
                self.charm.cluster_manager.update_primary_auth()
                # update the local unit admin password to match the leader
                self.charm.config_manager.update_local_valkey_admin()
            except (ValkeyACLLoadError, ValkeyConfigSetError, ValkeyWorkloadCommandError) as e:
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
            secret_content = self.charm.state.get_secret_from_id(secret_id, refresh=True)
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
                self.charm.cluster_manager.update_primary_auth()
                self.charm.state.cluster.update(
                    {
                        f"{user.value.replace('-', '_')}_password": new_passwords[user.value]
                        for user in CharmUsers
                    }
                )
                # update the local unit admin password
                self.charm.config_manager.update_local_valkey_admin()
            except (
                ValkeyACLLoadError,
                ValueError,
                ValkeyConfigSetError,
                ValkeyWorkloadCommandError,
            ) as e:
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
