#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all sentinel related tasks."""

import logging

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope
from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

from common.client import ValkeyClient
from common.exceptions import (
    CannotSeeAllActiveSentinelsError,
    SentinelFailoverError,
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

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_sentinel_discovered(self) -> bool:
        """Check if the sentinel of the local unit was discovered by the other sentinels in the cluster."""
        # list of active sentinels: units with started flag true and not being removed
        active_sentinels = [
            unit.model.private_ip
            for unit in self.state.servers
            if unit.is_active and unit.model.private_ip != self.state.bind_address
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
                if self.state.bind_address not in output:
                    logger.info(f"Sentinel at {sentinel_ip} has not discovered this sentinel")
                    return False
            except ValkeyWorkloadCommandError:
                logger.warning(f"Could not query sentinel at {sentinel_ip} for primary discovery.")
                return False
        return True

    def get_primary_ip(self) -> str | None:
        """Get the IP address of the primary node in the cluster."""
        started_servers = [unit for unit in self.state.servers if unit.is_active]

        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
            connect_to="sentinel",
        )

        for unit in started_servers:
            if primary_ip := client.sentinel_get_primary_ip(hostname=unit.model.private_ip):
                logger.info(f"Primary IP address is {primary_ip}")
                return primary_ip
        logger.error(
            "Could not determine primary IP from sentinels. Number of started servers: %d.",
            len(started_servers),
        )
        return None

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_healthy(self) -> bool:
        """Check if the sentinel service is healthy."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
            connect_to="sentinel",
        )

        if not client.ping(hostname=self.state.bind_address):
            logger.warning("Health check failed: Sentinel did not respond to ping.")
            return False

        if not client.sentinel_get_master_info(hostname=self.state.bind_address):
            logger.warning("Health check failed: Could not query sentinel for master information.")
            return False

        return True

    def failover(self) -> None:
        """Trigger a failover in the cluster."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
            connect_to="sentinel",
        )
        try:
            client.sentinel_failover(self.state.bind_address)
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to trigger failover: {e}")
            raise SentinelFailoverError from e

    def reset_sentinel_states(self) -> None:
        """Reset the sentinel states on all sentinels in the cluster."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
            connect_to="sentinel",
        )

        active_sentinels = [unit for unit in self.state.servers if unit.is_active]
        logger.debug(
            "Resetting sentinel states on %s", str([unit.unit_name for unit in active_sentinels])
        )
        for unit in active_sentinels:
            try:
                client.sentinel_reset_state(hostname=unit.model.private_ip)
            except ValkeyWorkloadCommandError:
                logger.warning(
                    f"Could not reset sentinel state on {unit.unit_name} ({unit.model.private_ip})."
                )
                raise

            if not self.sentinel_sees_all_others(target_sentinel_ip=unit.model.private_ip):
                logger.warning(
                    f"Sentinel at {unit.model.private_ip} does not see all other sentinels after reset."
                )
                raise CannotSeeAllActiveSentinelsError(
                    f"Sentinel at {unit.model.private_ip} does not see all other sentinels after reset."
                )

    @retry(
        wait=wait_fixed(1),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def sentinel_sees_all_others(self, target_sentinel_ip: str) -> bool:
        """Check if the sentinel of the local unit sees all the other sentinels in the cluster."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
            connect_to="sentinel",
        )

        other_active_sentinels = [
            unit.model.private_ip
            for unit in self.state.servers
            if unit.is_active and unit.model.private_ip != target_sentinel_ip
        ]

        logger.debug(
            "Checking if sentinel at %s sees all other sentinels: %s",
            target_sentinel_ip,
            other_active_sentinels,
        )

        for sentinel_ip in other_active_sentinels:
            try:
                output, _ = client.exec_cli_command(
                    command=["sentinel", "sentinels", PRIMARY_NAME],
                    hostname=target_sentinel_ip,
                )
                if sentinel_ip not in output:
                    logger.debug(
                        f"Sentinel at {target_sentinel_ip} does not see sentinel at {sentinel_ip}"
                    )
                    return False
            except ValkeyWorkloadCommandError:
                logger.warning(
                    f"Could not query sentinel at {target_sentinel_ip} for sentinel discovery."
                )
                return False
        return True

    def verify_expected_replica_count(self) -> bool:
        """Verify that the sentinels in the cluster see the expected number of replicas."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
            connect_to="sentinel",
        )

        units_started = [unit for unit in self.state.servers if unit.is_active]
        # all started servers except primary are expected to be replicas
        expected_replicas = len(units_started) - 1
        logger.debug(
            "Verifying expected replica count. Expected replicas: %d, started servers: %s",
            expected_replicas,
            str([unit.unit_name for unit in units_started]),
        )
        try:
            for unit in units_started:
                replica_info = client.sentinel_get_replica_info(hostname=unit.model.private_ip)
                if expected_replicas != (nbr_replicas := replica_info.count("name")):
                    logger.warning(
                        f"Sentinel at {unit.model.private_ip} sees {nbr_replicas} replicas, expected {expected_replicas}."
                    )
                    return False
        except ValkeyWorkloadCommandError:
            logger.warning("Could not query sentinel for replica information.")
            return False
        return True

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the sentinel manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope, component=self.name, running_status_only=True, running_status_type="async"
        ).root

        return status_list or [CharmStatuses.ACTIVE_IDLE.value]
