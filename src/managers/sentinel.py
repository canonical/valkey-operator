#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all sentinel related tasks."""

import logging

import tenacity
from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from common.client import ValkeyClient
from common.exceptions import (
    ValkeyWorkloadCommandError,
)
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import PRIMARY_NAME, CharmUsers
from statuses import CharmStatuses

logger = logging.getLogger(__name__)


class SentinelManager(ManagerStatusProtocol):
    """Manage sentinel members."""

    name: str = "sentinel"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload
        self.admin_user = CharmUsers.SENTINEL_CHARM_ADMIN.value

    @property
    def admin_password(self) -> str:
        """Get the password of the admin user for the sentinel service."""
        return self.state.cluster.internal_users_credentials.get(
            CharmUsers.SENTINEL_CHARM_ADMIN.value, ""
        )

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
            and unit.is_started
            and unit.model.private_ip != self.state.unit_server.model.private_ip
        ]

        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
            connect_to="sentinel",
        )

        for sentinel_ip in active_sentinels:
            try:
                output, _ = client.exec_cli_command(
                    command=["sentinel", "sentinels", PRIMARY_NAME],
                    hostname=sentinel_ip,
                )
                if self.state.unit_server.model.private_ip not in output:
                    logger.info(f"Sentinel at {sentinel_ip} has not discovered this sentinel")
                    return False
            except ValkeyWorkloadCommandError:
                logger.warning(f"Could not query sentinel at {sentinel_ip} for primary discovery.")
                continue
        return True

    def get_primary_ip(self) -> str | None:
        """Get the IP address of the primary node in the cluster."""
        started_servers = [unit for unit in self.state.servers if unit.model and unit.is_started]

        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
            connect_to="sentinel",
        )

        for unit in started_servers:
            try:
                output = client.exec_cli_command(
                    command=["sentinel", "get-master-addr-by-name", PRIMARY_NAME],
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

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the sentinel manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope, component=self.name, running_status_only=True, running_status_type="async"
        ).root

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
