#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed k8s operator for Valkey."""

import logging

import ops
from common.core.cluster_state import ClusterState
from common.events.base_events import BaseEvents
from common.managers.cluster import ClusterManager
from common.managers.config import ConfigManager
from data_platform_helpers.advanced_statuses.handler import StatusHandler

from literals import CONTAINER
from workload import ValkeyK8sWorkload

logger = logging.getLogger(__name__)


class ValkeyK8sCharm(ops.CharmBase):
    """Charmed Operator for Valkey K8s."""

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

        # --- Observers
        self.framework.observe(self.on.valkey_pebble_ready, self._on_pebble_ready)

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent) -> None:
        """Handle the `pebble-ready` event."""
        if not self.workload.can_connect:
            logger.warning("Container not ready yet")
            event.defer()
            return

        if not self.unit.is_leader():
            logger.warning("Scaling not implemented yet, services not started")
            return

        self.config_manager.set_config_properties()
        self.workload.start()
        logger.info("Services started")
        self.state.unit_server.update({"started": True})


if __name__ == "__main__":
    ops.main(ValkeyK8sCharm)
