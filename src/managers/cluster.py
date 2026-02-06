#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all cluster related tasks."""

import logging

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyConfigSetError,
    ValkeyWorkloadCommandError,
)
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import CharmUsers
from statuses import CharmStatuses

logger = logging.getLogger(__name__)


class ClusterManager(ManagerStatusProtocol):
    """Manage cluster members, authorization and other server related tasks."""

    name: str = "cluster"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload
        self.admin_user = CharmUsers.VALKEY_ADMIN.value
        self.admin_password = self.state.unit_server.valkey_admin_password
        # target only the unit's valkey server IP
        self.cluster_ips = [self.workload.get_private_ip()]

    def reload_acl_file(self) -> None:
        """Reload the ACL file into the cluster."""
        try:
            self._exec_cli_command(["acl", "load"])
        except ValkeyWorkloadCommandError:
            raise ValkeyACLLoadError("Could not load ACL file into Valkey cluster.")

    def update_primary_auth(self) -> None:
        """Update the primaryauth runtime configuration on the Valkey server."""
        if self.state.unit_server.model.private_ip == self.state.cluster.model.primary_ip:
            logger.info("Current unit is primary; no need to update primaryauth")
            return
        try:
            self._exec_cli_command(
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

    def _exec_cli_command(self, command: list[str]) -> str:
        """Execute a Valkey CLI command on the server."""
        cli_command = [
            "valkey-cli",
            "--user",
            self.admin_user,
            "--pass",
            self.admin_password,
        ] + command
        output = self.workload.exec(cli_command)
        logger.debug("Executed command: %s, got output: %s", " ".join(command), output)
        return output

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the cluster manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope, component=self.name, running_status_only=True, running_status_type="async"
        ).root

        if not self.workload.can_connect:
            status_list.append(CharmStatuses.SERVICE_NOT_STARTED.value)

        if not self.state.unit_server.is_started:
            status_list.append(CharmStatuses.SERVICE_NOT_STARTED.value)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
