#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all cluster related tasks."""

import logging

import tenacity
from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from common.client import ValkeyClient
from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyConfigSetError,
    ValkeyWorkloadCommandError,
)
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import CharmUsers, StartState
from statuses import CharmStatuses, ClusterStatuses

logger = logging.getLogger(__name__)


class ClusterManager(ManagerStatusProtocol):
    """Manage cluster members, authorization and other server related tasks."""

    name: str = "cluster"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload
        self.admin_user = CharmUsers.VALKEY_ADMIN.value

    @property
    def admin_password(self) -> str:
        """Get the password of the admin user for the Valkey cluster."""
        return self.state.unit_server.valkey_admin_password

    def reload_acl_file(self) -> None:
        """Reload the ACL file into the cluster."""
        try:
            client = ValkeyClient(
                username=self.admin_user,
                password=self.admin_password,
                workload=self.workload,
            )
            client.exec_cli_command(["acl", "load"])
        except ValkeyWorkloadCommandError:
            raise ValkeyACLLoadError("Could not load ACL file into Valkey cluster.")

    def update_primary_auth(self) -> None:
        """Update the primaryauth runtime configuration on the Valkey server."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
        )
        try:
            client.exec_cli_command(
                [
                    "config",
                    "set",
                    "primaryauth",
                    self.state.cluster.internal_users_credentials.get(
                        CharmUsers.VALKEY_REPLICA.value, ""
                    ),
                ],
            )
            logger.info("Updated primaryauth runtime configuration on Valkey server")
        except ValkeyWorkloadCommandError:
            raise ValkeyConfigSetError("Could not set primaryauth on Valkey server.")

    @tenacity.retry(
        wait=tenacity.wait_fixed(5),
        stop=tenacity.stop_after_attempt(5),
        retry=tenacity.retry_if_result(lambda result: result is False),
        reraise=True,
    )
    def is_replica_synced(self) -> bool:
        """Check if the replica is synced with the primary."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
        )
        try:
            output = (
                client.exec_cli_command(
                    command=["role"],
                )[0]
                .strip()
                .split()
            )
            if output and output[0] == "slave" and output[3] == "connected":
                logger.info("Replica is synced with primary")
                return True

            return False
        except ValkeyWorkloadCommandError:
            logger.warning("Could not determine replica sync status from Valkey server.")
            return False

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the cluster manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope, component=self.name, running_status_only=True, running_status_type="async"
        ).root

        if not self.workload.can_connect:
            status_list.append(CharmStatuses.SERVICE_NOT_STARTED.value)

        # Peer relation not established yet, or model not built yet for unit or app
        if not self.state.cluster.model or not self.state.unit_server.model:
            return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]

        if self.state.charm.unit.is_leader():
            return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]

        # non leader statuses
        if (
            not self.state.cluster.internal_users_credentials
            or not self.state.number_units_started
        ):
            status_list.append(
                ClusterStatuses.WAITING_FOR_PRIMARY_START.value,
            )

        match self.state.unit_server.model.start_state:
            case StartState.NOT_STARTED.value:
                status_list.append(
                    CharmStatuses.WAITING_TO_START.value,
                )
            case StartState.STARTING_WAITING_SENTINEL.value:
                status_list.append(
                    ClusterStatuses.WAITING_FOR_SENTINEL_DISCOVERY.value,
                )
            case StartState.STARTING_WAITING_REPLICA_SYNC.value:
                status_list.append(
                    ClusterStatuses.WAITING_FOR_REPLICA_SYNC.value,
                )

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
