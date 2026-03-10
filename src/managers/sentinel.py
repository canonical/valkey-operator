#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all sentinel related tasks."""

import logging

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope
from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

from common.client import SentinelClient
from common.exceptions import (
    CannotSeeAllActiveSentinelsError,
    SentinelFailoverError,
    SentinelIncorrectReplicaCountError,
    ValkeyCannotGetPrimaryIPError,
    ValkeyWorkloadCommandError,
)
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import CharmUsers
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

    def _get_sentinel_client(self) -> SentinelClient:
        """Get a client connection to Sentinel."""
        return SentinelClient(
            username=self.admin_user,
            password=self.admin_password,
            tls=self.state.unit_server.is_tls_enabled,
            workload=self.workload,
        )

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(6),
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

        client = self._get_sentinel_client()

        for sentinel_ip in active_sentinels:
            try:
                discovered_sentinels = {
                    sentinel["ip"] for sentinel in client.sentinels_primary(hostname=sentinel_ip)
                }
                if self.state.bind_address not in discovered_sentinels:
                    logger.warning(
                        f"Sentinel at {sentinel_ip} does not see local sentinel at {self.state.bind_address}."
                    )
                    return False

            except ValkeyWorkloadCommandError:
                logger.warning(f"Could not query sentinel at {sentinel_ip} for primary discovery.")
                return False
        return True

    def get_primary_ip(self) -> str:
        """Get the IP address of the primary node in the cluster.

        This method queries the sentinels in the cluster for the primary information and returns the primary's IP address.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command to get primary information fails on all sentinels.
        """
        started_servers = [unit.model.private_ip for unit in self.state.servers if unit.is_active]

        client = self._get_sentinel_client()

        for unit_ip in started_servers:
            try:
                return client.get_primary_addr_by_name(hostname=unit_ip)[0]
            except ValkeyWorkloadCommandError:
                logger.warning(
                    "Could not query sentinel for primary information from server at %s.",
                    unit_ip,
                )
                continue
        logger.error(
            "Could not determine primary IP from sentinels: %s.",
            started_servers,
        )
        raise ValkeyCannotGetPrimaryIPError("Could not determine primary IP from sentinels.")

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(6),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_healthy(self) -> bool:
        """Check if the sentinel service is healthy."""
        client = self._get_sentinel_client()

        if not client.ping(hostname=self.state.bind_address):
            logger.warning("Health check failed: Sentinel did not respond to ping.")
            return False

        try:
            client.primary(hostname=self.state.bind_address)
        except ValkeyWorkloadCommandError:
            logger.warning("Health check failed: Could not query sentinel for master information.")
            return False

        return True

    def failover(self) -> None:
        """Trigger a failover in the cluster.

        This method triggers a failover through the sentinel client and then checks if the failover is in progress.

        Raises:
            SentinelFailoverError: If triggering failover fails or if failover does not start after triggering.
        """
        client = self._get_sentinel_client()
        try:
            client.failover_primary_coordinated(self.state.bind_address)
            client.is_failover_in_progress(hostname=self.state.bind_address)
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to trigger failover: {e}")
            raise SentinelFailoverError from e

    def reset_sentinel_states(self, sentinel_ips: list[str]) -> None:
        """Reset the sentinel states on all sentinels in the cluster.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command to reset sentinel state fails on any sentinel.
            CannotSeeAllActiveSentinelsError: If any sentinel does not see all other active sentinels after reset.
        """
        client = self._get_sentinel_client()

        for sentinel_ip in sentinel_ips:
            logger.debug("Resetting sentinel state on %s.", sentinel_ip)
            client.reset(hostname=sentinel_ip)

            if not self.target_sees_all_others(
                target_sentinel_ip=sentinel_ip, sentinel_ips=sentinel_ips
            ):
                logger.warning(
                    "Sentinel at %s does not see all other sentinels after reset.", sentinel_ip
                )
                raise CannotSeeAllActiveSentinelsError(
                    "Sentinel at %s does not see all other sentinels after reset." % sentinel_ip
                )

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(6),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def target_sees_all_others(self, target_sentinel_ip: str, sentinel_ips: list[str]) -> bool:
        """Check if the sentinel of the local unit sees all the other sentinels in the cluster.

        Args:
            target_sentinel_ip: The IP address of the sentinel to check.
            sentinel_ips: The list of IP addresses of all active sentinels in the cluster.

        Returns:
            bool: True if the target sentinel sees all other sentinels, False otherwise.
        """
        client = self._get_sentinel_client()

        sentinel_ips_set = set(sentinel_ips) - {target_sentinel_ip}

        logger.debug(
            "Checking if sentinel at %s sees all other sentinels: %s",
            target_sentinel_ip,
            sentinel_ips_set,
        )

        try:
            discovered_sentinels = {
                sentinel["ip"]
                for sentinel in client.sentinels_primary(hostname=target_sentinel_ip)
            }
            if discovered_sentinels != sentinel_ips_set:
                logger.warning(
                    f"Sentinel at {target_sentinel_ip} sees sentinels {discovered_sentinels}, expected {sentinel_ips_set}."
                )
                return False
        except ValkeyWorkloadCommandError:
            logger.warning(
                f"Could not query sentinel at {target_sentinel_ip} for sentinel discovery."
            )
            return False
        return True

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def verify_expected_replica_count(self, sentinel_ips: list[str]) -> None:
        """Verify that the sentinels in the cluster see the expected number of replicas.

        The expected number of replicas is the number of active sentinels minus one (the primary).

        Args:
            sentinel_ips: The list of IP addresses of all active sentinels in the cluster.

        Raises:
            SentinelIncorrectReplicaCountError: If any sentinel sees an incorrect number of replicas.
            ValkeyWorkloadCommandError: If the CLI command to get replica information fails on any sentinel.
        """
        client = self._get_sentinel_client()

        # all started servers except primary are expected to be replicas
        expected_replicas = len(sentinel_ips) - 1
        logger.debug(
            "Verifying expected replica count. Expected replicas: %d, active servers: %s",
            expected_replicas,
            sentinel_ips,
        )

        for sentinel_ip in sentinel_ips:
            if expected_replicas != (
                number_replicas := len(client.replicas_primary(hostname=sentinel_ip))
            ):
                logger.warning(
                    f"Sentinel at {sentinel_ip} sees {number_replicas} replicas, expected {expected_replicas}."
                )
                raise SentinelIncorrectReplicaCountError(
                    f"Sentinel at {sentinel_ip} sees {number_replicas} replicas, expected {expected_replicas}."
                )

    def get_active_sentinel_ips(self, hostname: str) -> list[str]:
        """Get a list of IP addresses of the active sentinels in the cluster.

        Args:
            hostname: The hostname to query the sentinels from.

        Returns:
            list[str]: A list of IP addresses of the active sentinels.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command to get sentinel information fails.
        """
        client = self._get_sentinel_client()
        return [hostname] + [
            sentinel["ip"] for sentinel in client.sentinels_primary(hostname=hostname)
        ]

    def restart_service(self) -> None:
        """Restart the sentinel service to load configuration."""
        logger.info("Restarting sentinel service")
        self.workload.restart(self.workload.sentinel_service)

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the sentinel manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope, component=self.name, running_status_only=True, running_status_type="async"
        ).root

        return status_list or [CharmStatuses.ACTIVE_IDLE.value]
