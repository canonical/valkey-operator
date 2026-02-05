#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""Manager for handling TLS-related operations."""

import logging

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from statuses import CharmStatuses

logger = logging.getLogger(__name__)


class TLSManager(ManagerStatusProtocol):
    """Manage all TLS related events."""

    name: str = "tls"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the TLS statuses."""
        status_list: list[StatusObject] = []

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
