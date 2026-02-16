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
    StartState,
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
        The Valkey service is running and the current node is in sync with the primary (if a replica).
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
        """Handle the on start event."""
        if not self.charm.workload.can_connect:
            logger.warning("Workload not ready yet")
            event.defer()
            return
        self.charm.state.unit_server.update({"start_state": StartState.NOT_STARTED.value})

        primary_ip = self.charm.sentinel_manager.get_primary_ip()
        if self.charm.unit.is_leader() and not primary_ip:
            self._start_services(event, primary_ip=self.charm.state.bind_address)
            logger.info("Services started")
            self.charm.state.unit_server.update({"start_state": StartState.STARTED.value})
            return

        if not self.charm.state.cluster.internal_users_credentials or not primary_ip:
            logger.info(
                "Non-leader unit waiting for leader to set primary and internal user credentials"
            )
            event.defer()
            return

        self.charm.state.unit_server.update({"request_start_lock": True})

        # TODO unit.name would not work across models we need to switch to using `model.unit.name + model_uuid`
        if self.charm.state.cluster.model.starting_member != self.charm.unit.name:
            logger.info("Non-leader unit waiting for leader to choose it as starting member")
            event.defer()
            return

        if not self._start_services(event, primary_ip=primary_ip):
            return
        self.unit_fully_started.emit()

    def _start_services(self, event: ops.StartEvent, primary_ip: str) -> bool:
        """Start Valkey and Sentinel services."""
        try:
            self.charm.config_manager.update_local_valkey_admin_password()
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
            return False
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
        if not self.charm.workload.alive():
            logger.error("Workload failed to start.")
            self.charm.status.set_running_status(
                ValkeyServiceStatuses.SERVICE_NOT_RUNNING.value,
                scope="unit",
                component_name=self.charm.cluster_manager.name,
                statuses_state=self.charm.state.statuses,
            )
            return False

        logger.info("Workload started successfully. Opening client port")
        self.charm.unit.open_port("tcp", CLIENT_PORT)
        self.charm.state.statuses.delete(
            ValkeyServiceStatuses.SERVICE_STARTING.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )

        self.charm.state.statuses.delete(
            ValkeyServiceStatuses.SERVICE_NOT_RUNNING.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )
        return True

    # TODO check how to trigger if deferred without update status event
    def _on_unit_fully_started(self, event: UnitFullyStarted) -> None:
        """Handle the unit-fully-started event."""
        # Only ran on non-leader units when starting replicas
        if not self.charm.sentinel_manager.is_sentinel_discovered():
            logger.info("Sentinel service not yet discovered by other units. Deferring event.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_SENTINEL.value}
            )
            event.defer()
            return

        if not self.charm.cluster_manager.is_replica_synced():
            logger.info("Replica not yet synced. Deferring event.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_REPLICA_SYNC.value}
            )
            event.defer()
            return

        logger.info("Services started")
        self.charm.state.unit_server.update(
            {"start_state": StartState.STARTED.value, "request_start_lock": False}
        )

    def _on_peer_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle event received by all units when a unit's relation data changes."""
        if not self.charm.unit.is_leader():
            return

        units_requesting_start = [
            unit.unit_name
            for unit in self.charm.state.servers
            if unit.model and unit.model.request_start_lock
        ]
        starting_unit = next(
            (
                unit
                for unit in self.charm.state.servers
                if unit.unit_name == self.charm.state.cluster.model.starting_member
            ),
            None,
        )
        if (
            # if the starting member has not started yet, we want to wait for it to start instead of choosing another unit that requested start
            self.charm.state.cluster.model.starting_member
            and starting_unit
            and not starting_unit.is_started
        ):
            logger.debug(
                "Starting member %s has not started yet. Units requesting start: %s. ",
                self.charm.state.cluster.model.starting_member,
                units_requesting_start,
            )

        self.charm.state.cluster.update(
            {"starting_member": units_requesting_start[0] if units_requesting_start else ""}
        )

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
                "private_ip": self.charm.state.bind_address,
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
        self.charm.config_manager.update_local_valkey_admin_password()
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
                "private_ip": self.charm.state.bind_address,
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
                self.charm.config_manager.update_local_valkey_admin_password()
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
                self.charm.config_manager.update_local_valkey_admin_password()
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
