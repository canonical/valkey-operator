#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all cluster related tasks."""

import logging
from typing import Literal

import tenacity
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

    @property
    def number_units_started(self) -> int:
        """Return the number of units in the cluster that have their Valkey server started."""
        return len([unit for unit in self.state.servers if unit.model and unit.model.started])

    def reload_acl_file(self) -> None:
        """Reload the ACL file into the cluster."""
        try:
            self._exec_cli_command(["acl", "load"])
        except ValkeyWorkloadCommandError:
            raise ValkeyACLLoadError("Could not load ACL file into Valkey cluster.")

    def update_primary_auth(self) -> None:
        """Update the primaryauth runtime configuration on the Valkey server."""
        if self.get_primary_ip() == self.state.unit_server.model.private_ip:
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

    @tenacity.retry(
        wait=tenacity.wait_fixed(5),
        stop=tenacity.stop_after_attempt(5),
        retry=tenacity.retry_if_result(lambda result: result is False),
        reraise=True,
    )
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

    @tenacity.retry(
        wait=tenacity.wait_fixed(5),
        stop=tenacity.stop_after_attempt(5),
        retry=tenacity.retry_if_result(lambda result: result is False),
        reraise=True,
    )
    def is_replica_synced(self) -> bool:
        """Check if the replica is synced with the primary."""
        if self.get_primary_ip() == self.state.unit_server.model.private_ip:
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

    def get_primary_ip(self) -> str | None:
        """Get the IP address of the primary node in the cluster."""
        started_servers = [
            unit for unit in self.state.servers if unit.model and unit.model.started
        ]

        for unit in started_servers:
            try:
                output = self._exec_cli_command(
                    ["sentinel", "get-master-addr-by-name", PRIMARY_NAME],
                    connect_to="sentinel",
                    hostname=unit.model.private_ip,
                )[0]
                primary_ip = output.strip().split()[0]
                logger.info(f"Primary IP address is {primary_ip}")
                return primary_ip
            except (IndexError, ValkeyWorkloadCommandError):
                logger.error("Could not get primary IP from sentinel output.")

        logger.error(
            "Could not determine primary IP from sentinels. Number of started servers: %d.",
            len(started_servers),
        )

    def _exec_cli_command(
        self,
        command: list[str],
        hostname: str | None = None,
        connect_to: Literal["valkey", "sentinel"] = "valkey",
    ) -> tuple[str, str | None]:
        """Execute a Valkey CLI command on the server.

        Args:
            command (list[str]): The CLI command to execute, as a list of arguments.
            hostname (str | None): The hostname to connect to. Defaults to private ip of unit.
            connect_to (Literal["valkey", "sentinel"]): Whether to connect to the valkey server or sentinel for executing the command. Defaults to "valkey".

        Returns:
            tuple[str, str | None]: The standard output and standard error from the command execution.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute.
        """
        if not hostname:
            hostname = self.workload.get_private_ip()
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
            self.workload.cli,
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
        logger.debug("Executed command: %s, got output: %s", " ".join(command[0]), output)
        if error:
            logger.error("Error output from command '%s': %s", " ".join(command[0]), error)
        return output, error

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the cluster manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope, component=self.name, running_status_only=True, running_status_type="async"
        ).root

        if not self.workload.can_connect:
            status_list.append(CharmStatuses.SERVICE_NOT_STARTED.value)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
