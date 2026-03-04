#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all cluster related tasks."""

import logging

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope
from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

from common.client import ValkeyClient
from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyConfigSetError,
)
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import CharmUsers, StartState, TLSState
from statuses import CharmStatuses, StartStatuses

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

    def _get_valkey_client(self) -> ValkeyClient:
        """Get a client connection to Valkey."""
        return ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            tls=True
            if self.state.unit_server.tls_client_state in [TLSState.TLS, TLSState.TO_NO_TLS]
            else False,
            workload=self.workload,
        )

    def reload_acl_file(self) -> None:
        """Reload the ACL file into the cluster."""
        client = self._get_valkey_client()
        if not client.load_acl(hostname=self.state.bind_address):
            raise ValkeyACLLoadError("Could not load ACL file into Valkey cluster.")

    def update_primary_auth(self) -> None:
        """Update the primaryauth runtime configuration on the Valkey server."""
        client = self._get_valkey_client()
        if not client.config_set(
            hostname=self.state.bind_address,
            parameter="primaryauth",
            value=self.state.cluster.internal_users_credentials.get(
                CharmUsers.VALKEY_REPLICA.value, ""
            ),
        ):
            raise ValkeyConfigSetError("Could not set primaryauth on Valkey server.")

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_replica_synced(self) -> bool:
        """Check if the replica is synced with the primary."""
        client = self._get_valkey_client()
        return client.is_replica_synced(hostname=self.state.bind_address)

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_healthy(self, is_primary: bool = False, check_replica_sync: bool = True) -> bool:
        """Check if a valkey instance is healthy."""
        client = self._get_valkey_client()

        if not client.ping(hostname=self.state.bind_address):
            logger.warning("Health check failed: Valkey server did not respond to ping.")
            return False

        if (
            persistence_info := client.get_persistence_info(hostname=self.state.bind_address)
        ) and persistence_info.get("loading", "") != "0":
            logger.warning("Health check failed: Valkey server is still loading data.")
            return False

        if not is_primary and check_replica_sync and not self.is_replica_synced():
            logger.warning("Health check failed: Replica is not synced with primary.")
            return False

        return True

    def reload_tls_settings(self, tls_config: dict[str, str]) -> None:
        """Update TLS by loading the TLS settings."""
        client = self._get_valkey_client()
        client.reload_tls(tls_config, hostname=self.state.bind_address)

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the cluster manager's statuses."""
        status_list: list[StatusObject] = []

        # Peer relation not established yet, or model not built yet for unit or app
        if not self.state.cluster.model or not self.state.unit_server.model:
            return status_list or [CharmStatuses.ACTIVE_IDLE.value]

        match self.state.unit_server.model.start_state:
            case StartState.NOT_STARTED.value:
                status_list.append(
                    StartStatuses.SERVICE_NOT_STARTED.value,
                )
            case StartState.WAITING_FOR_PRIMARY_START.value:
                status_list.append(
                    StartStatuses.WAITING_FOR_PRIMARY_START.value,
                )
            case StartState.WAITING_TO_START.value:
                status_list.append(
                    StartStatuses.WAITING_TO_START.value,
                )
            case StartState.CONFIGURATION_ERROR.value:
                status_list.append(
                    StartStatuses.CONFIGURATION_ERROR.value,
                )
            case StartState.STARTING_WAITING_VALKEY.value:
                status_list.append(
                    StartStatuses.SERVICE_STARTING.value,
                )
            case StartState.STARTING_WAITING_SENTINEL.value:
                status_list.append(
                    StartStatuses.WAITING_FOR_SENTINEL_DISCOVERY.value,
                )
            case StartState.STARTING_WAITING_REPLICA_SYNC.value:
                status_list.append(
                    StartStatuses.WAITING_FOR_REPLICA_SYNC.value,
                )
            case StartState.ERROR_ON_START.value:
                status_list.append(
                    StartStatuses.ERROR_ON_START.value,
                )

        return status_list or [CharmStatuses.ACTIVE_IDLE.value]
