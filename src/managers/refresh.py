#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for computing upgrades related statuses."""

import logging

import charm_refresh
import ops
from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import Substrate
from statuses import CharmStatuses, ClusterStatuses

logger = logging.getLogger(__name__)


class RefreshManager(ManagerStatusProtocol):
    """Manage refresh statuses."""

    name: str = "refresh"
    state: ClusterState

    def __init__(
        self, state: ClusterState, workload: WorkloadBase, refresh: charm_refresh.Common | None
    ):
        self.state = state  # pyright: ignore[reportIncompatibleVariableOverride]
        self.workload = workload
        self.refresh = refresh

    @property
    def refresh_in_progress(self) -> bool:
        """Check if charm-refresh is currently in progress."""
        if not self.refresh:
            return False

        return self.refresh.in_progress

    def workload_allowed_to_start(self) -> bool:
        """Check if the workload is allowed to start from refresh-perspective."""
        # relevant for K8s only
        if self.state.substrate == Substrate.VM:
            return True

        if not self.refresh:
            return False

        return self.refresh.workload_allowed_to_start

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the upgrades-relevant statuses.

        Advanced Statuses defines the status priority order per component. It is not possible to
        have some statuses of a component more important and some other statuses of the same
        component less important.

        While `refresh.[app|unit]_status_higher_priority` must be of higher priority than any other
        status, `refresh.unit_status_lower_priority()` should only be set it if there is no other
        status at all. We achieve this by setting the field `approved_critical_component` to True
        if the refresh_status is of higher priority than any other status.

        For more information: see https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/status/
        """
        status_list: list[StatusObject] = []

        if not self.refresh:
            return [CharmStatuses.ACTIVE_IDLE.value]

        if scope == "app" and (refresh_app_status := self.refresh.app_status_higher_priority):
            app_status = self._convert_ops_status_to_advanced_status(refresh_app_status)
            status_list.append(app_status)
            return status_list

        if self.refresh.in_progress and not self.refresh.next_unit_allowed_to_refresh:
            status_list.append(ClusterStatuses.UNHEALTHY_AFTER_REFRESH.value)

        if refresh_unit_status := self.refresh.unit_status_higher_priority:
            unit_status = self._convert_ops_status_to_advanced_status(refresh_unit_status)
            status_list.append(unit_status)

        if refresh_lower_unit_status := self.refresh.unit_status_lower_priority(
            workload_is_running=self.workload.alive()
        ):
            lower_unit_status = self._convert_ops_status_to_advanced_status(
                refresh_lower_unit_status, critical=False
            )
            status_list.append(lower_unit_status)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]

    @staticmethod
    def _convert_ops_status_to_advanced_status(
        ops_status: ops.StatusBase, critical: bool = True
    ) -> StatusObject:
        """Convert an ops status into an advanced statuses StatusObject.

        Args:
            ops_status (ops.StatusBase): the status to convert into an advanced status
            critical (bool): whether the returned StatusObject should have the field
                            `approved_critical_component` set to True or False
        """
        # this code may not be very concise, focus is on readability
        match ops_status:
            case ops.BlockedStatus():
                return StatusObject(
                    status="blocked",
                    message=ops_status.message,
                    approved_critical_component=critical,
                )

            case ops.MaintenanceStatus():
                return StatusObject(
                    status="maintenance",
                    message=ops_status.message,
                    approved_critical_component=critical,
                )

            case ops.WaitingStatus():
                return StatusObject(
                    status="waiting",
                    message=ops_status.message,
                    approved_critical_component=critical,
                )

            case ops.ActiveStatus():
                return StatusObject(status="active", message=ops_status.message)

            case _:
                raise ValueError(f"Unknown status type: {ops_status.name}: {ops_status.message}")
