#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed k8s operator for Valkey."""

import logging
import os

import charm_refresh
import ops
import ops.log
from data_platform_helpers.advanced_statuses.handler import StatusHandler

from common.custom_events import RestartWorkloadEvent, TopologyChangedCharmEvents
from common.exceptions import (
    ValkeyServicesFailedToStartError,
    ValkeyUpgradeError,
    ValkeyWorkloadCommandError,
)
from common.locks import RestartLock
from core.cluster_state import ClusterState
from events.backup import BackupEvents
from events.base_events import BaseEvents
from events.external_clients import ExternalClientsEvents
from events.ldap import LDAPEvents
from events.observability import ObservabilityEvents
from events.refresh import K8sValkeyRefresh, MachinesValkeyRefresh
from events.tls import TLSEvents
from literals import CONTAINER, PEER_RELATION, Substrate
from managers.auth import AuthManager
from managers.backup import BackupManager
from managers.cluster import ClusterManager
from managers.config import ConfigManager
from managers.external_clients import ExternalClientsManager
from managers.metrics import MetricsManager
from managers.refresh import RefreshManager
from managers.sentinel import SentinelManager
from managers.tls import TLSManager
from managers.topology import TopologyManager
from workload_k8s import ValkeyK8sWorkload
from workload_vm import ValkeyVmWorkload

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class ValkeyCharm(ops.CharmBase):
    """Charmed Operator for Valkey."""

    restart_workload = ops.EventSource(RestartWorkloadEvent)
    # Overriding `on` with a custom CharmEvents subclass is the intended ops API;
    # pyright flags it only because `CharmBase.on` is declared as a property.
    on = TopologyChangedCharmEvents()  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]

    def __init__(self, *args) -> None:
        super().__init__(*args)
        # Show logger name (module name) in logs
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if isinstance(handler, ops.log.JujuLogHandler):
                handler.setFormatter(logging.Formatter("{name}:{message}", style="{"))

        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            try:
                self.model.get_cloud_spec()
            except ops.ModelError:
                logger.error("The `valkey` application must be deployed with the `--trust` flag.")
                raise
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
        self.client_manager = ExternalClientsManager(state=self.state, workload=self.workload)
        self.topology_manager = TopologyManager(state=self.state, workload=self.workload)
        self.auth_manager = AuthManager(state=self.state, workload=self.workload)
        self.backup_manager = BackupManager(state=self.state, workload=self.workload)
        self.metrics_manager = MetricsManager(state=self.state, workload=self.workload)

        # --- UPGRADES ---
        try:
            if self.substrate == Substrate.K8S:
                self.refresh = charm_refresh.Kubernetes(
                    K8sValkeyRefresh(
                        workload_name="Valkey",
                        charm_name="valkey",
                        oci_resource_name="valkey-image",
                        charm=self,
                    )
                )
            else:
                self.refresh = charm_refresh.Machines(
                    MachinesValkeyRefresh(workload_name="Valkey", charm_name="valkey", charm=self)
                )
        except (
            charm_refresh.UnitTearingDown,
            charm_refresh.PeerRelationNotReady,
            charm_refresh.KubernetesJujuAppNotTrusted,
        ):
            self.refresh = None

        self.refresh_manager = RefreshManager(
            state=self.state, workload=self.workload, refresh=self.refresh
        )

        # --- STATUS HANDLER ---
        self.status = StatusHandler(
            self,
            self.refresh_manager,
            self.cluster_manager,
            self.config_manager,
            self.auth_manager,
            self.sentinel_manager,
            self.tls_manager,
            self.client_manager,
            self.backup_manager,
            self.metrics_manager,
        )

        # --- EVENT HANDLERS ---
        self.base_events = BaseEvents(self)
        self.tls_events = TLSEvents(self)
        self.client_events = ExternalClientsEvents(self)
        self.backup_events = BackupEvents(self)
        self.ldap_events = LDAPEvents(self)
        self.observability_events = ObservabilityEvents(self)

        self.framework.observe(self.restart_workload, self._on_restart_workload)

        # ensure that post refresh handling is executed in EVERY hook
        self.post_refresh_handling(self.refresh)

    def post_refresh_handling(self, refresh: charm_refresh.Common | None) -> None:
        """Handle post refresh steps like start and health checks."""
        if not refresh or refresh.next_unit_allowed_to_refresh:
            return

        if not self.state.unit_server.is_started:
            logger.debug("Scaled up units must go through start event")
            return

        if not refresh.workload_allowed_to_start:
            logger.debug("Workload not allowed to start")
            return

        if self.workload.alive():
            if not (
                self.cluster_manager.is_healthy(is_primary=self.cluster_manager.is_primary())
                and self.sentinel_manager.is_healthy()
            ):
                # to ensure the unit receives another Juju event
                raise ValkeyUpgradeError("Workload not healthy yet")

            logger.info("Unit is healthy, allowing next unit to refresh")
            refresh.next_unit_allowed_to_refresh = True
            return

        logger.info("Restarting workload")
        if self.app.planned_units() == 1:
            # there is no other sentinel to query
            primary_ip = self.state.unit_server.get_endpoint(self.state.substrate)
        else:
            primary_ip = self.sentinel_manager.get_primary_ip()
        self.auth_manager.configure_auth()
        self.config_manager.configure_services(primary_ip)
        self.metrics_manager.reconcile()
        self.workload.start()

        # config sets min-replicas-to-write=1; reassert the correct value now the server is up
        self.cluster_manager.reconcile_min_replicas_to_write()
        if self.unit.is_leader() and self.state.substrate == Substrate.K8S:
            self.sentinel_manager.set_pod_labels()

        logger.info("Confirming health after upgrade")
        if not (
            self.cluster_manager.is_healthy(is_primary=self.cluster_manager.is_primary())
            and self.sentinel_manager.is_healthy()
        ):
            # to ensure the unit receives another Juju event
            raise ValkeyUpgradeError("Workload not healthy yet")

        logger.info("Unit is healthy, allowing next unit to refresh")
        refresh.next_unit_allowed_to_refresh = True

    def _on_restart_workload(self, event: RestartWorkloadEvent) -> None:
        """Handle the restart_workload event."""
        logger.info(
            "Restarting workload Event. Restart Valkey: %s, Restart Sentinel: %s",
            event.restart_valkey,
            event.restart_sentinel,
        )
        if (
            self.state.unit_server.is_backup_in_progress
            or self.state.cluster.is_restore_in_progress
        ):
            logger.info("Backup/restore in progress on this unit; deferring restart_workload")
            event.defer()
            return
        restart_lock = RestartLock(self.state)
        restart_lock.request_lock()
        if not restart_lock.is_held_by_this_unit:
            logger.info("Waiting for lock to restart workload")
            event.defer()
            return

        try:
            if event.restart_valkey:
                self.workload.restart(self.workload.valkey_service)
            if event.restart_sentinel:
                # if primary endpoint is given, write sentinel config
                # this is necessary as Sentinel may rewrite its config file since the last write
                if event.primary_endpoint != "":
                    self.config_manager.set_sentinel_config_properties(
                        primary_endpoint=event.primary_endpoint
                    )
                self.sentinel_manager.restart_service()
        except (
            ValkeyServicesFailedToStartError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error(e)
            restart_lock.release_lock()
            event.defer()
            return

        if event.restart_valkey and not self.cluster_manager.is_healthy(check_replica_sync=False):
            self.state.unit_server.update({"is_valkey_healthy": False})
            restart_lock.release_lock()
            event.defer()
            return
        self.state.unit_server.update({"is_valkey_healthy": True})

        if event.restart_valkey:
            # CONFIG SET min-replicas-to-write does not survive the restart we
            # just performed; the rendered file ships 1, so reassert the
            # topology-correct runtime value (0 on < 3 active units) now that
            # Valkey is back up and healthy, or a small cluster would be
            # write-frozen until the next peer-relation event.
            self.cluster_manager.reconcile_min_replicas_to_write()

        if event.restart_sentinel and not self.sentinel_manager.is_healthy():
            self.state.unit_server.update({"is_sentinel_healthy": False})
            restart_lock.release_lock()
            event.defer()
            return

        self.state.unit_server.update({"is_sentinel_healthy": True})
        restart_lock.release_lock()

    def trigger_relation_change_if_required(self) -> None:
        """Trigger a peer-relation changed event if it is a single-unit deployment."""
        if len(self.state.servers) != 1:
            return

        logger.debug("Trigger a relation-changed event in a single-unit deployment")
        self.on[PEER_RELATION].relation_changed.emit(self.state.peer_relation)


if __name__ == "__main__":
    ops.main(ValkeyCharm)
