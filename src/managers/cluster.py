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
from literals import CharmUsers, ScaleDownState, StartState
from statuses import CharmStatuses, ScaleDownStatuses, StartStatuses

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
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
        )
        if not client.acl_load(hostname=self.state.bind_address):
            raise ValkeyACLLoadError("Could not load ACL file into Valkey cluster.")

    def update_primary_auth(self) -> None:
        """Update the primaryauth runtime configuration on the Valkey server."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
        )
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
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
        )
        role_info = client.role(hostname=self.state.bind_address)
        return role_info[0] == "slave" and role_info[3] == "connected"

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_healthy(self, is_primary: bool = False, check_replica_sync: bool = True) -> bool:
        """Check if a valkey instance is healthy."""
        client = ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            workload=self.workload,
        )

        if not client.ping(hostname=self.state.bind_address):
            logger.warning("Health check failed: Valkey server did not respond to ping.")
            return False

        if (
            persistence_info := client.info_persistence(hostname=self.state.bind_address)
        ) and persistence_info.get("loading", "") != "0":
            logger.warning("Health check failed: Valkey server is still loading data.")
            return False

        if not is_primary and check_replica_sync and not self.is_replica_synced():
            logger.warning("Health check failed: Replica is not synced with primary.")
            return False

        return True

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the cluster manager's statuses."""
        status_list: list[StatusObject] = []

        # Peer relation not established yet, or model not built yet for unit or app
        if not self.state.cluster.model or not self.state.unit_server.model:
            return status_list or [CharmStatuses.ACTIVE_IDLE.value]

        if scope == "unit":
            if start_status := self._get_start_status():
                status_list.append(start_status)

            if scale_down_status := self._get_scale_down_status():
                status_list.append(scale_down_status)

        return status_list or [CharmStatuses.ACTIVE_IDLE.value]

    def _get_start_status(self) -> StatusObject | None:
        """Get the current start status of the unit."""
        match self.state.unit_server.model.start_state:
            case StartState.NOT_STARTED.value:
                if (
                    self.state.unit_server.model.scale_down_state
                    == ScaleDownState.NO_SCALE_DOWN.value
                ):
                    return StartStatuses.SERVICE_NOT_STARTED.value
            case StartState.WAITING_FOR_PRIMARY_START.value:
                return StartStatuses.WAITING_FOR_PRIMARY_START.value
            case StartState.WAITING_TO_START.value:
                return StartStatuses.WAITING_TO_START.value
            case StartState.CONFIGURATION_ERROR.value:
                return StartStatuses.CONFIGURATION_ERROR.value
            case StartState.STARTING_WAITING_VALKEY.value:
                return StartStatuses.SERVICE_STARTING.value
            case StartState.STARTING_WAITING_SENTINEL.value:
                return StartStatuses.WAITING_FOR_SENTINEL_DISCOVERY.value
            case StartState.STARTING_WAITING_REPLICA_SYNC.value:
                return StartStatuses.WAITING_FOR_REPLICA_SYNC.value
            case StartState.ERROR_ON_START.value:
                return StartStatuses.ERROR_ON_START.value

        return None

    def _get_scale_down_status(self) -> StatusObject | None:
        """Get the current scale down status of the unit."""
        match self.state.unit_server.model.scale_down_state:
            case ScaleDownState.GOING_AWAY.value:
                return ScaleDownStatuses.GOING_AWAY.value

        return None
