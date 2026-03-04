#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed k8s operator for Valkey."""

import logging

import ops
from data_platform_helpers.advanced_statuses.handler import StatusHandler

from core.cluster_state import ClusterState
from events.base_events import BaseEvents
from events.tls import TLSEvents
from literals import CONTAINER, Substrate
from managers.cluster import ClusterManager
from managers.config import ConfigManager
from managers.sentinel import SentinelManager
from managers.tls import TLSManager
from workload_k8s import ValkeyK8sWorkload
from workload_vm import ValkeyVmWorkload

logger = logging.getLogger(__name__)


class ValkeyCharm(ops.CharmBase):
    """Charmed Operator for Valkey."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        try:
            cloud_spec = self.model.get_cloud_spec()
        except ops.ModelError:
            logger.error("Application must be deployed with `trust` to get cloud spec")
            raise

        if cloud_spec.type == "kubernetes":
            self.substrate = Substrate.K8S
            self.workload = ValkeyK8sWorkload(container=self.unit.get_container(CONTAINER))
        else:
            self.substrate = Substrate.VM
            self.workload = ValkeyVmWorkload()
        self.state = ClusterState(self, self.substrate)

        # --- MANAGERS ---
        self.cluster_manager = ClusterManager(state=self.state, workload=self.workload)
        self.config_manager = ConfigManager(state=self.state, workload=self.workload)
        self.sentinel_manager = SentinelManager(state=self.state, workload=self.workload)
        self.tls_manager = TLSManager(state=self.state, workload=self.workload)

        # --- STATUS HANDLER ---
        self.status = StatusHandler(
            self,
            self.cluster_manager,
            self.config_manager,
            self.sentinel_manager,
            self.tls_manager,
        )

        # --- EVENT HANDLERS ---
        self.base_events = BaseEvents(self)
        self.tls_events = TLSEvents(self)


if __name__ == "__main__":
    ops.main(ValkeyCharm)
