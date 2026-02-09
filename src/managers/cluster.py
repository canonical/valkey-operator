#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all cluster related tasks."""

import logging
from typing import Literal

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
from literals import CLIENT_PORT, PRIMARY_NAME, SENTINEL_PORT, CharmUsers
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

    def is_sentinel_discovered(self) -> bool:
        """Check if the sentinel of the local unit was discovered by the other sentinels in the cluster."""
        # list of active sentinels: units with started flag true
        active_sentinels = [
            unit.model.private_ip
            for unit in self.state.servers
            if unit.model
            and unit.model.started
            and unit.model.private_ip != self.state.unit_server.model.private_ip
        ]

        for sentinel_ip in active_sentinels:
            try:
                output, _ = self._exec_cli_command(
                    command=["sentinel", "sentinels", PRIMARY_NAME],
                    hostname=sentinel_ip,
                    connect_to="sentinel",
                )
                if self.state.unit_server.model.private_ip not in output:
                    logger.info(f"Sentinel at {sentinel_ip} has discovered this sentinel")
                    return False
            except ValkeyWorkloadCommandError:
                logger.warning(f"Could not query sentinel at {sentinel_ip} for primary discovery.")
                continue
        return True

    def is_replica_synced(self) -> bool:
        """Check if the replica is synced with the primary."""
        if self.state.unit_server.model.private_ip == self.state.cluster.model.primary_ip:
            logger.info("Current unit is primary; no need to check replica sync")
            return True
        try:
            output = (
                self._exec_cli_command(
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

    def _exec_cli_command(
        self,
        command: list[str],
        hostname: str = "localhost",
        connect_to: Literal["valkey", "sentinel"] = "valkey",
    ) -> tuple[str, str | None]:
        """Execute a Valkey CLI command on the server.

        Args:
            command (list[str]): The CLI command to execute, as a list of arguments.
            hostname (str): The hostname to connect to. Defaults to "localhost".
            connect_to (Literal["valkey", "sentinel"]): Whether to connect to the valkey server or sentinel for executing the command. Defaults to "valkey".

        Returns:
            tuple[str, str | None]: The standard output and standard error from the command execution.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute.
        """
        port = CLIENT_PORT if connect_to == "valkey" else SENTINEL_PORT
        user = (
            CharmUsers.VALKEY_ADMIN.value
            if connect_to == "valkey"
            else CharmUsers.SENTINEL_CHARM_ADMIN.value
        )
        password = (
            self.state.unit_server.valkey_admin_password
            if connect_to == "valkey"
            else self.state.cluster.internal_users_credentials.get(
                CharmUsers.SENTINEL_CHARM_ADMIN.value, ""
            )
        )
        cli_command = [
            "valkey-cli",
            "-h",
            hostname,
            "-p",
            str(port),
            "--user",
            user,
            "--pass",
            password,
        ] + command
        output, error = self.workload.exec(cli_command)
        logger.debug("Executed command: %s, got output: %s", " ".join(command), output)
        if error:
            logger.error("Error output from command '%s': %s", " ".join(command), error)
        return output, error

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the cluster manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope, component=self.name, running_status_only=True, running_status_type="async"
        ).root

        if not self.workload.can_connect:
            status_list.append(CharmStatuses.SERVICE_NOT_STARTED.value)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
