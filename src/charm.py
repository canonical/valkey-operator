#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed k8s operator for Valkey."""

import logging
import os
from typing import Any

import charm_refresh
import ops
import ops.log
from charmlibs import pathops
from charmlibs.rollingops import RollingOpsManager, RollingOpsNoRelationError
from data_platform_helpers.advanced_statuses.handler import StatusHandler

from common.custom_events import TopologyChangedCharmEvents
from common.exceptions import (
    SentinelFailoverError,
    ValkeyAOFArchiveNoSpaceError,
    ValkeyCannotGetPrimaryIPError,
    ValkeyClusterNotReadyError,
    ValkeyUpgradeError,
    ValkeyWorkloadCommandError,
)
from common.locks import ValkeyScaleDownLockBackend
from core.cluster_state import ClusterState
from events.backup import BackupEvents
from events.base_events import BaseEvents
from events.external_clients import ExternalClientsEvents
from events.ldap import LDAPEvents
from events.observability import ObservabilityEvents
from events.refresh import K8sValkeyRefresh, MachinesValkeyRefresh
from events.tls import TLSEvents
from literals import (
    CONTAINER,
    PEER_RELATION,
    RESTART_OPERATION_ID,
    ROLLINGOPS_BASE_DIR,
    ROLLINGOPS_PEER_RELATION,
    SCALE_DOWN_LOCK_ID,
    START_OPERATION_ID,
    VALKEY_DOWN_RUN_ID,
    ScaleDownState,
    StartState,
    Substrate,
)
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
from statuses import PersistenceStatuses
from workload_k8s import ValkeyK8sWorkload
from workload_vm import ValkeyVmWorkload

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class ValkeyCharm(ops.CharmBase):
    """Charmed Operator for Valkey."""

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

        # --- ROLLING OPS ---
        self.rollingops = RollingOpsManager(
            self,
            peer_relation_name=ROLLINGOPS_PEER_RELATION,
            callback_targets={
                RESTART_OPERATION_ID: self.base_events.restart_workload,
                START_OPERATION_ID: self.base_events.start_unit,
            },
            sync_lock_targets={SCALE_DOWN_LOCK_ID: ValkeyScaleDownLockBackend(self)},
            base_dir=pathops.LocalPath(ROLLINGOPS_BASE_DIR),
        )

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

        # a corrupted AOF fails the health checks below, which raise before update-status can heal it
        self.heal_corrupted_aof()

        if not self.workload.alive():
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

    def reconcile_persistence(self) -> None:
        """Move this unit to the stored persistence settings if it has not applied them yet.

        Failures are logged, never deferred, because a replay would block every later hook on
        the waits and update-status already retries.
        """
        persistence = self.state.cluster.persistence_config
        settings = self.config_manager.persistence_settings(persistence)
        applied = self.state.unit_server.applied_persistence_config
        # compare Valkey settings, so editing an option the mode doesn't use reapplies nothing
        if applied and self.config_manager.persistence_settings(applied) == settings:
            # a reverted failed switch can leave the new settings live
            try:
                if self.cluster_manager.is_live_persistence(settings):
                    return
            except ValkeyWorkloadCommandError:
                # a down server restarts on the rendered file, which matches
                return

        try:
            # resolved first, so a server switched live always gets its file rendered to match
            primary_endpoint = self.sentinel_manager.get_primary_ip()
            logger.info("Applying persistence mode %s", persistence.mode)
            # new on, file, old off: whatever valkey.conf names on a restart is still current
            self.cluster_manager.enable_new_persistence(settings)
            self.config_manager.set_config_properties(
                primary_endpoint=primary_endpoint, persistence=persistence
            )
            self.cluster_manager.disable_old_persistence(settings)
        except (
            ValkeyCannotGetPrimaryIPError,
            ValkeyClusterNotReadyError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Failed to apply the persistence settings: %s", e)
            self.state.statuses.add(
                PersistenceStatuses.NOT_APPLIED.value,
                scope="unit",
                component=self.cluster_manager.name,
            )
            return

        self.state.statuses.delete(
            PersistenceStatuses.NOT_APPLIED.value,
            scope="unit",
            component=self.cluster_manager.name,
        )
        self.state.unit_server.update({"applied_persistence_config": persistence})

    def heal_corrupted_aof(self) -> None:
        """Rebuild a unit whose AOF is corrupted with a full sync from the primary (DA290).

        A primary that Sentinel has not failed over yet gets a forced failover first.
        """
        unit = self.state.unit_server
        # a unit that never launched Valkey cannot have crashed on its AOF
        if (
            not unit.model
            or unit.model.start_state == StartState.NOT_STARTED.value
            or self.state.cluster.is_restore_in_progress
            or unit.model.scale_down_state != ScaleDownState.NO_SCALE_DOWN.value
        ):
            return

        try:
            if not self.cluster_manager.is_aof_corrupted():
                return
        except ValkeyWorkloadCommandError as e:
            logger.error("Cannot check the AOF: %s", e)
            return

        try:
            primary = self.sentinel_manager.get_primary_ip()
            if primary == self.state.endpoint:
                logger.warning("The primary has a corrupted AOF, forcing a failover")
                self.sentinel_manager.failover(force=True)
                primary = self.sentinel_manager.get_primary_ip()
            if primary == self.state.endpoint:
                raise SentinelFailoverError("Sentinel still reports this unit as primary")
            # healing from a down primary leaves this unit empty and promotable (DA291)
            if not self.cluster_manager.is_healthy(is_primary=True, hostname=primary):
                raise ValkeyClusterNotReadyError(f"The primary {primary} is not healthy")
            # rendered before the AOF moves, so an auto-restart comes back as a replica
            self.config_manager.set_config_properties(primary_endpoint=primary)
            self.cluster_manager.archive_corrupted_aof()
        except (
            SentinelFailoverError,
            ValkeyAOFArchiveNoSpaceError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyClusterNotReadyError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Cannot heal the corrupted AOF: %s", e)
            # not the status handler: VM `refresh_snap` heals before it exists
            self.state.statuses.add(
                self._aof_heal_status(e).value,
                scope="unit",
                component=self.cluster_manager.name,
            )
            return

        self._clear_aof_statuses()
        # no restart: the service manager already restarts the server, a second one aborts its sync

    @staticmethod
    def _aof_heal_status(error: Exception) -> PersistenceStatuses:
        """Pick the status for a failed heal.

        Args:
            error: The exception the heal raised.

        Returns:
            The status to show on the unit.
        """
        # once a human frees space, the next update-status retries
        if isinstance(error, ValkeyAOFArchiveNoSpaceError):
            return PersistenceStatuses.AOF_ARCHIVE_NO_SPACE
        if isinstance(error, ValkeyWorkloadCommandError):
            return PersistenceStatuses.AOF_HEAL_FAILED
        return PersistenceStatuses.AOF_CORRUPTED

    def _clear_aof_statuses(self) -> None:
        """Clear the corrupted-AOF statuses."""
        for status in (
            PersistenceStatuses.AOF_CORRUPTED,
            PersistenceStatuses.AOF_ARCHIVE_NO_SPACE,
            PersistenceStatuses.AOF_HEAL_FAILED,
        ):
            self.state.statuses.delete(
                status.value, scope="unit", component=self.cluster_manager.name
            )

    def trigger_relation_change_if_required(self) -> None:
        """Trigger a peer-relation changed event if it is a single-unit deployment."""
        if len(self.state.servers) != 1:
            return

        logger.debug("Trigger a relation-changed event in a single-unit deployment")
        self.on[PEER_RELATION].relation_changed.emit(self.state.peer_relation)

    def request_restart(self, **kwargs: Any) -> None:
        """Queue a rolling restart of this unit.

        Without the rollingops peer relation the unit is either not started yet, and its start
        applies the config, or it is leaving, so the restart is skipped.

        Args:
            **kwargs: Arguments for `BaseEvents.restart_workload`.
        """
        if kwargs.get("restart_valkey", True):
            # the callback restarts only this process, a newer one already runs the new config
            try:
                run_id = self.cluster_manager.run_id()
            except ValkeyWorkloadCommandError:
                # whatever process starts next reads the config already rendered
                run_id = VALKEY_DOWN_RUN_ID
            self.state.unit_server.update({"restart_run_id": run_id})
        try:
            self.rollingops.request_async_lock(RESTART_OPERATION_ID, kwargs=kwargs)
        except RollingOpsNoRelationError:
            logger.warning("No rollingops peer relation, skipping the restart request")


if __name__ == "__main__":
    ops.main(ValkeyCharm)
