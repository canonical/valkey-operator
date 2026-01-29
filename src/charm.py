#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed k8s operator for Valkey."""

import logging

import ops
from data_platform_helpers.advanced_statuses.handler import StatusHandler

from core.cluster_state import ClusterState
from events.base_events import BaseEvents
from literals import CHARM_USER, CLIENT_PORT, CONTAINER, DATA_DIR
from managers.cluster import ClusterManager
from managers.config import ConfigManager
from statuses import ValkeyServiceStatuses
from workload_k8s import ValkeyK8sWorkload

logger = logging.getLogger(__name__)


class ValkeyCharm(ops.CharmBase):
    """Charmed Operator for Valkey."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.workload = ValkeyK8sWorkload(container=self.unit.get_container(CONTAINER))
        self.state = ClusterState(self)

        # --- MANAGERS ---
        self.cluster_manager = ClusterManager(state=self.state, workload=self.workload)
        self.config_manager = ConfigManager(state=self.state, workload=self.workload)

        # --- STATUS HANDLER ---
        self.status = StatusHandler(
            self,
            self.cluster_manager,
            self.config_manager,
        )

        # --- EVENT HANDLERS ---
        self.base_events = BaseEvents(self)

        # --- Observers ---
        self.framework.observe(self.on.start, self._on_start)

    def _on_start(self, event: ops.StartEvent) -> None:
        """Handle the `start` event."""
        if not self.workload.can_connect:
            logger.warning("Container not ready yet")
            event.defer()
            return

        if not self.unit.is_leader() and (
            not self.state.cluster.internal_users_credentials
            or not self.state.cluster.model.primary_ip
        ):
            logger.info("Deferring leader write primary and internal user credentials")
            event.defer()
            return

        self.config_manager.set_config_properties()
        self.config_manager.set_acl_file()
        self.config_manager.set_sentinel_config_properties()
        self.config_manager.set_sentinel_acl_file()
        self.workload.mkdir(DATA_DIR, user=CHARM_USER, group=CHARM_USER)
        self.status.set_running_status(
            ValkeyServiceStatuses.SERVICE_STARTING.value,
            scope="unit",
            component_name=self.cluster_manager.name,
            statuses_state=self.state.statuses,
        )
        self.workload.start()
        if self.workload.alive():
            logger.info("Workload started successfully. Opening client port")
            self.unit.open_port("tcp", CLIENT_PORT)
            self.state.statuses.delete(
                ValkeyServiceStatuses.SERVICE_STARTING.value,
                scope="unit",
                component=self.cluster_manager.name,
            )
        else:
            logger.error("Workload failed to start.")
            self.status.set_running_status(
                ValkeyServiceStatuses.SERVICE_NOT_RUNNING.value,
                scope="unit",
                component_name=self.cluster_manager.name,
                statuses_state=self.state.statuses,
            )
        logger.info("Services started")
        self.state.unit_server.update({"started": True})


if __name__ == "__main__":
    ops.main(ValkeyCharm)
