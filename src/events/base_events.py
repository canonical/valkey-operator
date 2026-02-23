#!/usr/bin/env python3
# Copyright 2025 Canonical Limited
# See LICENSE file for licensing details.

"""Valkey base event handlers."""

import logging
import socket
from typing import TYPE_CHECKING

import ops

from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyConfigSetError,
    ValkeyConfigurationError,
    ValkeyServiceNotAliveError,
    ValkeyServicesFailedToStartError,
    ValkeyWorkloadCommandError,
)
from literals import (
    CLIENT_PORT,
    INTERNAL_USERS_PASSWORD_CONFIG,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    PEER_RELATION,
    CharmUsers,
    StartState,
    Substrate,
)
from statuses import CharmStatuses, ClusterStatuses, StartStatuses

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class UnitFullyStarted(ops.EventBase):
    """Event that signals that the unit's has fully started.

    This event will be deferred until:
        The Sentinel service is running and was discovered by other units.
        The Valkey service is running and the current node is in sync with the primary (if a replica).
    """

    def __init__(self, handle: ops.Handle, is_primary: bool = False):
        super().__init__(handle)
        self.is_primary = is_primary

    def snapshot(self) -> dict[str, str]:
        """Save the state of the event."""
        return {"is_primary": str(self.is_primary)}

    def restore(self, snapshot: dict[str, str]) -> None:
        """Restore the state of the event."""
        self.is_primary = snapshot.get("is_primary", "False") == "True"


class BaseEvents(ops.Object):
    """Handle all base events."""

    unit_fully_started = ops.EventSource(UnitFullyStarted)

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="base_events")
        self.charm = charm

        self.framework.observe(self.charm.on.install, self._on_install)
        self.framework.observe(self.charm.on.start, self._on_start)
        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_changed,
            self._on_peer_relation_changed,
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
        self.charm.state.unit_server.update({"start_state": StartState.NOT_STARTED.value})

        if not self.charm.workload.can_connect:
            logger.warning("Workload not ready yet")
            event.defer()
            return

        if not self.charm.state.cluster.internal_users_credentials:
            logger.info(
                "Internal users' credentials not set yet. Deferring start event until credentials are set."
            )
            event.defer()
            return

        self.charm.state.unit_server.update(
            {
                "start_state": StartState.WAITING_TO_START.value,
                "request_start_lock": True,
            }
        )

        if self.charm.unit.is_leader():
            logger.info(
                "Leader unit requesting lock to start services. Triggering lock request processing."
            )
            self._process_lock_requests()

        # TODO unit.name would not work across models we need to switch to using `model.unit.name + model_uuid`
        if self.charm.state.cluster.model.starting_member != self.charm.unit.name:
            logger.info("Waiting for lock to start")
            event.defer()
            return

        if not (primary_ip := self.charm.sentinel_manager.get_primary_ip()):
            if self.charm.state.number_units_started == 0 and self.charm.unit.is_leader():
                primary_ip = self.charm.state.bind_address
            else:
                logger.debug(
                    "Primary IP not available yet or other units have already started, deferring start event until leader starts the primary"
                )
                self.charm.state.unit_server.update(
                    {"start_state": StartState.WAITING_FOR_PRIMARY_START.value}
                )
                event.defer()
                return

        try:
            self.charm.config_manager.configure_services(primary_ip)
            self.charm.workload.start()
        except ValkeyConfigurationError:
            self.charm.state.unit_server.update(
                {
                    "start_state": StartState.CONFIGURATION_ERROR.value,
                    "request_start_lock": False,
                }
            )
            event.defer()
            return
        except (ValkeyServicesFailedToStartError, ValkeyServiceNotAliveError) as e:
            logger.error(e)
            self.charm.state.unit_server.update(
                {
                    "start_state": StartState.ERROR_ON_START.value,
                    "request_start_lock": False,
                }
            )
            event.defer()
            return

        self.charm.status.set_running_status(
            StartStatuses.SERVICE_STARTING.value,
            scope="unit",
            statuses_state=self.charm.state.statuses,
            component_name=self.charm.cluster_manager.name,
        )

        self.unit_fully_started.emit(is_primary=primary_ip == self.charm.state.bind_address)

    # TODO check how to trigger if deferred without update status event
    def _on_unit_fully_started(self, event: UnitFullyStarted) -> None:
        """Handle the unit-fully-started event."""
        if not self.charm.cluster_manager.is_healthy(
            is_primary=event.is_primary, check_replica_sync=False
        ):
            logger.warning("Unit is not healthy after start, deferring event.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_VALKEY.value}
            )
            event.defer()
            return

        if not self.charm.sentinel_manager.is_healthy():
            logger.warning("Sentinel is not healthy after start, deferring event.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_SENTINEL.value}
            )
            event.defer()
            return

        if not event.is_primary and not self.charm.sentinel_manager.is_sentinel_discovered():
            logger.info("Sentinel service not yet discovered by other units. Deferring event.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_SENTINEL.value}
            )
            event.defer()
            return

        if not event.is_primary and not self.charm.cluster_manager.is_replica_synced():
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
        # in case both units requests start before the leader is done
        if self.charm.unit.is_leader():
            self._process_lock_requests()

        self.charm.unit.open_port("tcp", CLIENT_PORT)

    def _on_peer_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle event received by all units when a unit's relation data changes."""
        if not self.charm.unit.is_leader():
            return

        self._process_lock_requests()

    def _process_lock_requests(self) -> None:
        """Process start lock requests.

        The leader unit will choose one of the units that requested the lock to start, and update the cluster model with that unit as the starting member.
        """
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
            return

        if units_requesting_start:
            self.charm.state.cluster.update({"starting_member": units_requesting_start[0]})
            logger.debug(f"Updated starting member to {units_requesting_start[0]}")
            return

        if self.charm.state.cluster.model.starting_member:
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
                # update the local unit admin password to match the leader
                self.charm.config_manager.update_local_valkey_admin_password()
                self.charm.cluster_manager.update_primary_auth()
            except (
                ValkeyACLLoadError,
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
                # update the local unit admin password
                self.charm.config_manager.update_local_valkey_admin_password()
                self.charm.cluster_manager.update_primary_auth()
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
