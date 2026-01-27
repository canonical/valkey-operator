#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all cluster related tasks."""

import logging

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from common.client import ValkeyClient
from common.exceptions import ValkeyACLLoadError
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
        self.admin_password = self.state.cluster.internal_users_credentials.get(
            CharmUsers.VALKEY_ADMIN.value, ""
        )
        self.cluster_hostnames = [server.model.hostname for server in self.state.servers]

    def load_acl_file(self) -> None:
        """Load the ACL file into the cluster."""
        try:
            client = ValkeyClient(
                username=self.admin_user,
                password=self.admin_password,
                hosts=self.cluster_hostnames,
            )
            client.load_acl()
        except ValkeyACLLoadError:
            raise

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the cluster manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope, component=self.name, running_status_only=True, running_status_type="async"
        ).root

        if not self.workload.can_connect:
            status_list.append(CharmStatuses.SERVICE_NOT_STARTED.value)

        if not self.state.unit_server.is_started:
            status_list.append(CharmStatuses.SCALING_NOT_IMPLEMENTED.value)

        if scope == "app":
            # todo: remove when scaling is implemented
            status_list.append(CharmStatuses.SCALING_NOT_IMPLEMENTED.value)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
