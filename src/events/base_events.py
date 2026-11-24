#!/usr/bin/env python3
# Copyright 2025 Canonical Limited
# See LICENSE file for licensing details.

"""Valkey base event handlers."""

import logging
import time
from typing import TYPE_CHECKING

import ops
from charmlibs.rollingops import OperationResult, RollingOpsNoRelationError
from pydantic import ValidationError

from common.exceptions import (
    CannotSeeAllActiveSentinelsError,
    NotAllDepartingSentinelsStoppedError,
    SentinelFailoverError,
    SentinelIncorrectReplicaCountError,
    ValkeyACLLoadError,
    ValkeyBackupInProgressError,
    ValkeyCannotGetPrimaryIPError,
    ValkeyConfigurationError,
    ValkeyServiceNotAliveError,
    ValkeyServicesCouldNotBeStoppedError,
    ValkeyServicesFailedToStartError,
    ValkeyWorkloadCommandError,
)
from core.models import PersistenceConfig
from literals import (
    ARCHIVE_STORAGE,
    CLIENT_PORT,
    DATA_STORAGE,
    INTERNAL_USERS_PASSWORD_CONFIG,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    LOG_STORAGE,
    METRICS_PORT,
    PEER_RELATION,
    REFRESH_PEER_RELATION,
    SCALE_DOWN_LOCK_ID,
    SCALE_DOWN_LOCK_TIMEOUT_S,
    SENTINEL_PORT,
    SENTINEL_TLS_PORT,
    START_MAX_HOLDS,
    START_OPERATION_ID,
    STARTING_STATES,
    TLS_PORT,
    VALKEY_DOWN_RUN_ID,
    CharmUsers,
    PersistenceMode,
    ScaleDownState,
    StartState,
    Substrate,
    TLSState,
)
from statuses import CharmStatuses, ClusterStatuses, ScaleDownStatuses

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class BaseEvents(ops.Object):
    """Handle all base events."""

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="base_events")
        self.charm = charm

        for storage_name in (DATA_STORAGE, LOG_STORAGE, ARCHIVE_STORAGE):
            self.framework.observe(
                self.charm.on[storage_name].storage_attached, self._on_storage_attached
            )
        self.framework.observe(self.charm.on.install, self._on_install)
        self.framework.observe(self.charm.on.start, self._on_start)
        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_changed, self._on_peer_relation_changed
        )
        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_departed, self._on_peer_relation_departed
        )
        self.framework.observe(self.charm.on.update_status, self._on_update_status)
        self.framework.observe(self.charm.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.charm.on.leader_elected, self._on_leader_elected)
        self.framework.observe(self.charm.on.config_changed, self._on_config_changed)
        self.framework.observe(
            self.charm.on[REFRESH_PEER_RELATION].relation_changed,
            self._on_refresh_peer_relation_changed,
        )
        self.framework.observe(self.charm.on.secret_changed, self._on_secret_changed)
        for storage_name in (DATA_STORAGE, LOG_STORAGE, ARCHIVE_STORAGE):
            self.framework.observe(
                self.charm.on[storage_name].storage_detaching, self._on_storage_detaching
            )

    def _on_storage_attached(self, event: ops.StorageAttachedEvent) -> None:
        """Set ownership/permissions on the attached storage dir."""
        storage_dirs = {
            DATA_STORAGE: self.charm.workload.working_dir,
            LOG_STORAGE: self.charm.workload.log_dir,
            ARCHIVE_STORAGE: self.charm.workload.archive_dir,
        }
        if (target := storage_dirs.get(event.storage.name)) is None:
            logger.warning("Unexpected storage %s attached; skipping", event.storage.name)
            return

        if event.storage.name == DATA_STORAGE:
            try:
                self.charm.cluster_manager.clean_up_inconsistent_dump_files()
            except ValkeyWorkloadCommandError:
                event.defer()
                return

        if self.charm.state.substrate == Substrate.K8S:
            return

        try:
            self.charm.workload.exec(["chmod", "-R", "750", target.as_posix()])
        except ValkeyWorkloadCommandError as e:
            logger.error("Error when setting storage permissions: %s", e)
            event.defer()
            return

    def _on_install(self, event: ops.InstallEvent) -> None:
        """Handle install event."""
        if self.charm.substrate == Substrate.K8S:
            if self.charm.unit.is_leader():
                logger.info("Create services for primary and replica endpoints")
                self.charm.sentinel_manager.reconcile_k8s_services()
            return

        try:
            self.charm.workload.install()  # pyright: ignore[reportAttributeAccessIssue]
        except RuntimeError:
            raise RuntimeError("Failed to install the Valkey snap")

    def _on_start(self, event: ops.StartEvent) -> None:
        """Handle the on start event."""
        try:
            self._ensure_workload_ready()
        except (RuntimeError, ValueError) as e:
            logger.warning(e)
            event.defer()
            return

        # The refresh handling already restarted the workload and checked its health.
        # We do not check refresh_in_progress as it is already false when the last unit starts.
        if self.charm.state.unit_server.is_started and self.charm.workload.alive():
            logger.info("Unit restarted by the refresh, not starting it again")
            return

        if self.charm.state.substrate == Substrate.VM:
            try:
                # The unit has not started yet. It can start with the new address directly
                self.charm.state.unit_server.update(
                    {"private_ip": self.charm.state.bind_address, "pending_ip": ""}
                )
            except ValueError:
                logger.warning("No address in the bound space, starting on the recorded one")

        if not self._start_conditions_met():
            event.defer()
            return

        self.charm.state.unit_server.update({"start_state": StartState.NOT_STARTED.value})

        if not self.charm.state.cluster.internal_users_credentials:
            logger.info(
                "Internal users' credentials not set yet. Deferring start event until credentials are set."
            )
            event.defer()
            return

        if self._get_primary_endpoint_for_start() is None:
            logger.debug("Primary not available, deferring start event")
            self.charm.state.unit_server.update(
                {"start_state": StartState.WAITING_FOR_PRIMARY_START.value}
            )
            event.defer()
            return

        self.charm.state.unit_server.update({"start_state": StartState.WAITING_TO_START.value})
        try:
            self.charm.rollingops.request_async_lock(START_OPERATION_ID)
        except RollingOpsNoRelationError:
            logger.info("No rollingops peer relation yet, deferring the start")
            event.defer()

    def _start_conditions_met(self) -> bool:
        """Check what must hold both when the start is requested and when the lock is granted.

        Returns:
            True if the workload is reachable, the client certificates are ready and the refresh
            allows the workload to start.
        """
        if not self.charm.workload.can_connect:
            logger.warning("Workload not ready yet")
            return False

        if len(self.charm.state.servers) != self.charm.app.planned_units():
            # ensure we can see all units in the peer relation, which we rely on later in the flow
            logger.warning("Peer relation not ready for all units")
            return False

        if (
            self.charm.state.client_tls_relation
            and not self.charm.state.unit_server.model.client_cert_ready
        ):
            logger.warning("Waiting for client TLS certificates before starting")
            return False

        # avoid accidental start of scaled-up unit during refresh
        # call goes through refresh manager to avoid unnecessary waits on VM
        if not self.charm.refresh_manager.workload_allowed_to_start():
            logger.warning("Refresh in progress, workload not allowed to start")
            return False

        return True

    def _get_primary_endpoint_for_start(self) -> str | None:
        """Find the primary a starting unit should follow.

        Returns:
            The primary endpoint, or None if there is no primary to follow yet.
        """
        try:
            return self.charm.sentinel_manager.get_primary_ip()
        except ValkeyCannotGetPrimaryIPError:
            logger.debug("No active primary for startup")

        if self.charm.state.number_units_started != 0:
            logger.debug("Another unit is already started, waiting for primary")
            return None

        if self.charm.app.planned_units() == 1:
            logger.debug("Single unit deployment, continue starting with this unit as primary")
            return self.charm.state.unit_server.get_endpoint(self.charm.state.substrate)

        persistence_config = self.charm.config_manager.get_current_persistence_config()
        if persistence_config.mode == PersistenceMode.NONE:
            if self.charm.unit.is_leader():
                logger.debug("No persistence enabled, continue starting with this unit as primary")
                return self.charm.state.unit_server.get_endpoint(self.charm.state.substrate)
            return None

        logger.debug("Check if local data exists")
        self.charm.cluster_manager.read_local_data_offset(persistence_config)

        if (
            self.charm.state.any_unit_local_data_offset_stored
            and not self.charm.state.all_units_local_data_offset_stored
        ):
            logger.debug("Not all units have read their local data, cannot determine primary yet")
            return None

        if latest_data_offset := self.charm.state.latest_data_offset:
            logger.debug("Latest data is: %s", latest_data_offset)
            if latest_data_offset == self.charm.state.unit_server.get_local_data_offset():
                logger.debug("Unit has latest data and will start as primary")
                return self.charm.state.unit_server.get_endpoint(self.charm.state.substrate)
            return None

        logger.debug("Cannot determine primary from data on disk")
        if self.charm.unit.is_leader():
            logger.debug("Starting the leader unit as primary")
            return self.charm.state.unit_server.get_endpoint(self.charm.state.substrate)
        return None

    def start_unit(self) -> OperationResult:
        """Start the workload once the rolling lock is granted to this unit.

        This is the rollingops `start` callback.

        Returns:
            RELEASE once the unit is started and in sync, RETRY_HOLD while its started services
            are not in sync yet, so no restart or other start runs in between, RETRY_RELEASE if
            it cannot start the services yet. After START_MAX_HOLDS holds in a row it returns
            RETRY_RELEASE once so queued restarts can run.
        """
        result = self._run_start()
        holds = 0
        if result == OperationResult.RETRY_HOLD:
            holds = self.charm.state.unit_server.model.start_hold_count + 1
        if holds >= START_MAX_HOLDS:
            logger.warning("Start held the lock %d times, releasing it once", holds)
            result, holds = OperationResult.RETRY_RELEASE, 0
        self.charm.state.unit_server.update({"start_hold_count": holds})
        return result

    def _run_start(self) -> OperationResult:
        """Run one start attempt, see `start_unit`."""
        if self.charm.state.unit_server.is_started:
            return OperationResult.RELEASE

        # once the services are started, keep the lock until the unit is in sync
        starting = self.charm.state.unit_server.model.start_state in STARTING_STATES
        retry = OperationResult.RETRY_HOLD if starting else OperationResult.RETRY_RELEASE

        if not self._start_conditions_met():
            return retry

        if self.charm.state.cluster.is_restore_in_progress:
            logger.info("Restore in progress, retrying the start later")
            return retry

        if not starting:
            primary_endpoint = self._get_primary_endpoint_for_start()
            if primary_endpoint is None:
                logger.info("Primary IP not available yet, retrying the start later")
                self.charm.state.unit_server.update(
                    {"start_state": StartState.WAITING_FOR_PRIMARY_START.value}
                )
                return OperationResult.RETRY_RELEASE

            if not self._start_workload(primary_endpoint):
                return OperationResult.RETRY_RELEASE

        if not self._is_started_and_in_sync():
            return OperationResult.RETRY_HOLD

        self._finish_start()
        return OperationResult.RELEASE

    def _start_workload(self, primary_endpoint: str) -> bool:
        """Configure and start the services.

        Args:
            primary_endpoint: Address of the primary to follow.

        Returns:
            True if the services were started, False otherwise.
        """
        # taken before the ACL write, so users added during start stay out of date
        acl_epoch = time.time()
        try:
            self.charm.auth_manager.configure_auth()
            self.charm.config_manager.configure_services(primary_endpoint)
            self.charm.metrics_manager.reconcile()
            self.charm.workload.start()
        except ValkeyConfigurationError:
            # the managers already set CONFIGURATION_ERROR
            return False
        except (ValkeyServicesFailedToStartError, ValkeyServiceNotAliveError) as e:
            logger.error(e)
            self.charm.state.unit_server.update({"start_state": StartState.ERROR_ON_START.value})
            return False

        self.charm.state.unit_server.update(
            {"start_state": StartState.STARTING_WAITING_VALKEY.value}
        )
        if self.charm.state.external_client_relations and not self.charm.unit.is_leader():
            self.charm.state.unit_server.update({"client_user_epoch": acl_epoch})
        return True

    def _is_started_and_in_sync(self) -> bool:
        """Check the started services, recording the start state the unit is still waiting in.

        Returns:
            True once Valkey and Sentinel are healthy and a replica is discovered and synced.
        """
        if not self.charm.cluster_manager.is_healthy(check_replica_sync=False):
            logger.warning("Unit is not healthy after start, waiting for Valkey.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_VALKEY.value}
            )
            return False

        # read the role live: Sentinel may have promoted this unit since it started
        try:
            is_primary = self.charm.cluster_manager.is_primary()
        except ValkeyWorkloadCommandError as e:
            logger.warning("Could not read the Valkey role, waiting for Valkey: %s", e)
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_VALKEY.value}
            )
            return False

        if not self.charm.sentinel_manager.is_healthy():
            logger.warning("Sentinel is not healthy after start, waiting for Sentinel.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_SENTINEL.value}
            )
            return False

        if not is_primary and not self.charm.sentinel_manager.is_sentinel_discovered():
            logger.info("Sentinel service not yet discovered by other units.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_SENTINEL.value}
            )
            return False

        if not is_primary and not self.charm.cluster_manager.is_replica_synced():
            logger.info("Replica not yet synced.")
            self.charm.state.unit_server.update(
                {"start_state": StartState.STARTING_WAITING_REPLICA_SYNC.value}
            )
            return False

        return True

    def _finish_start(self) -> None:
        """Run the repeatable post-start steps, then mark the unit started.

        The peer relation handlers triggered at the end skip units that are not started, so
        STARTED is written right before them and not after.
        """
        # the rendered config ships min-replicas-to-write=1; reassert the
        # topology-correct runtime value now the server is up, as CONFIG SET
        # does not persist across a restart
        self.charm.cluster_manager.reconcile_min_replicas_to_write()

        if self.charm.state.unit_server.tls_client_state != TLSState.TLS:
            self.charm.unit.open_port("tcp", CLIENT_PORT)
            self.charm.unit.open_port("tcp", SENTINEL_PORT)
        self.charm.unit.open_port("tcp", TLS_PORT)
        self.charm.unit.open_port("tcp", SENTINEL_TLS_PORT)
        if self.charm.state.substrate == Substrate.K8S:
            self.charm.unit.open_port("tcp", METRICS_PORT)

        logger.info("Services started")
        self.charm.state.unit_server.update(
            {
                "start_state": StartState.STARTED.value,
                "local_data_offset": "",
            }
        )

        # rerun peer handlers skipped while this unit was starting
        self.charm.on[PEER_RELATION].relation_changed.emit(self.charm.state.peer_relation)

        if not self.charm.unit.is_leader():
            return

        try:
            self.charm.topology_manager.start_observer()
        except (ValkeyWorkloadCommandError, ValueError) as e:
            logger.error("Failed to start topology observer: %s", e)

    def _on_peer_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle event received by all units when a unit's relation data changes."""
        if not self.charm.unit.is_leader():
            self.charm.topology_manager.stop_observer()

        if self.charm.state.cluster.is_restore_in_progress:
            return

        try:
            self._reconfigure_quorum_if_necessary()
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to update sentinel quorum: {e}")
            # not critical to defer here, we can wait for the next relation change

        if self.charm.state.unit_server.is_started:
            # reassert min-replicas-to-write to match the (possibly changed) topology
            self.charm.cluster_manager.reconcile_min_replicas_to_write()
            self.charm.metrics_manager.reconcile()
            self.charm.reconcile_persistence()

        if not self.charm.unit.is_leader():
            return

        if not self.charm.state.unit_server.is_active:
            return

        try:
            self.charm.sentinel_manager.remove_departed_sentinels_from_cluster()
        except (
            CannotSeeAllActiveSentinelsError,
            NotAllDepartingSentinelsStoppedError,
            SentinelIncorrectReplicaCountError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error(e)
            event.defer()
            return

        # return early during TLS switchover to avoid unnecessary operation during rolling restart for sentinel
        if self.charm.state.unit_server.model.tls_client_state in (
            TLSState.TO_TLS,
            TLSState.TO_NO_TLS,
        ):
            return

        # need to pick up scaling operations, TLS switchover, CA rotation and so on
        try:
            self.charm.topology_manager.restart_observer()
        except (ValkeyWorkloadCommandError, ValueError) as e:
            logger.error("Failed to restart topology observer: %s", e)

    def _on_peer_relation_departed(self, _: ops.RelationDepartedEvent) -> None:
        """Handle event received by all units when a unit departs."""
        # Shouldn't happen mid-restore (storage-detaching refuses scale-down); if
        # it does, return rather than defer (deferring a departed event is unsafe).
        if self.charm.state.cluster.is_restore_in_progress:
            return

        try:
            self._reconfigure_quorum_if_necessary()
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to update sentinel quorum: {e}")
            # not critical to defer here, we can wait for the next relation change

        # a unit departed (scale down): relax min-replicas-to-write if we dropped below 3
        if self.charm.state.unit_server.is_started:
            self.charm.cluster_manager.reconcile_min_replicas_to_write()

        if not self.charm.unit.is_leader():
            return

        # trigger Sentinel reset and health check, executed on relation-changed
        self.charm.state.cluster.update({"sentinel_reset_required": True})

        if not self.charm.state.unit_server.is_active:
            return

        try:
            self.charm.topology_manager.restart_observer()
        except (ValkeyWorkloadCommandError, ValueError) as e:
            logger.error("Failed to restart topology observer: %s", e)

    def _on_update_status(self, event: ops.UpdateStatusEvent) -> None:
        """Handle the update-status event."""
        self.charm.heal_corrupted_aof()

        if not self.charm.state.unit_server.is_started:
            logger.warning("Service not started")
            return

        self.charm.metrics_manager.reconcile()

        if not self.charm.state.cluster.is_restore_in_progress:
            self.charm.reconcile_persistence()

        self._request_address_move()

        if not self.charm.unit.is_leader():
            # fallback if peer-relation-changed missed the leader change
            self.charm.topology_manager.stop_observer()
            return

        try:
            self.charm.topology_manager.start_observer()
        except (ValkeyWorkloadCommandError, ValueError) as e:
            logger.error("Failed to start topology observer: %s", e)

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        """Restart the topology observer so it runs the new charm code."""
        if not self.charm.unit.is_leader() or not self.charm.state.unit_server.is_active:
            return

        try:
            self.charm.topology_manager.restart_observer()
        except (ValkeyWorkloadCommandError, ValueError) as e:
            logger.error("Failed to restart topology observer: %s", e)

    def _on_leader_elected(self, event: ops.LeaderElectedEvent) -> None:
        """Handle the leader-elected event."""
        try:
            self._ensure_workload_ready()
        except (RuntimeError, ValueError) as e:
            logger.warning(e)
            event.defer()
            return

        if not self.charm.unit.is_leader():
            return

        # fires peer-relation-changed on the old leader, so it stops its observer
        self.charm.state.cluster.update({"topology_observer_unit": self.charm.unit.name})

        # the old leader may have missed the config-changed
        self._store_persistence_config()

        if self.charm.state.unit_server.is_active:
            try:
                self.charm.sentinel_manager.remove_departed_sentinels_from_cluster()
            except (
                CannotSeeAllActiveSentinelsError,
                NotAllDepartingSentinelsStoppedError,
                SentinelIncorrectReplicaCountError,
                ValkeyCannotGetPrimaryIPError,
                ValkeyWorkloadCommandError,
            ) as e:
                logger.error(e)
                event.defer()
                return

            try:
                self.charm.topology_manager.start_observer()
            except (ValkeyWorkloadCommandError, ValueError) as e:
                logger.error("Failed to start topology observer: %s", e)

        if self.charm.state.cluster.internal_users_credentials:
            logger.debug("Internal user credentials already set")
            return

        passwords = {}
        user_specified_passwords = {}
        if admin_secret_id := self.charm.config.get(INTERNAL_USERS_PASSWORD_CONFIG):
            try:
                user_specified_passwords = self.charm.state.get_secret_from_id(
                    str(admin_secret_id)
                )
            except (ops.ModelError, ops.SecretNotFoundError) as e:
                logger.error("Could not access secret %s: %s", admin_secret_id, e)
                raise

        # generate passwords for all internal users if not specified in the user secret
        for user in CharmUsers:
            passwords[user.value] = user_specified_passwords.get(
                user.value, self.charm.auth_manager.generate_password()
            )

        self.charm.state.cluster.update(
            {
                f"{user.value.replace('-', '_')}_password": passwords[user.value]
                for user in CharmUsers
            }
        )
        # update local unit admin password
        self.charm.auth_manager.update_local_valkey_admin_password()

    def _ensure_workload_ready(self):
        """Ensure the peer relation is available and the workload container is ready."""
        if not self.charm.state.peer_relation:
            raise RuntimeError("Peer relation not ready yet")

        if not self.charm.workload.can_connect:
            raise RuntimeError("Workload container not ready yet")

        data = {"hostname": self.charm.state.hostname}
        # start and leader-elected also fire mid-move, so only the move may change a recorded address
        if (
            self.charm.state.substrate != Substrate.VM
            or not self.charm.state.unit_server.model.private_ip
        ):
            # this raises a ValueError if it fails
            data["private_ip"] = self.charm.state.bind_address
        self.charm.state.unit_server.update(data)

    def _request_address_move(self) -> None:
        """Record a new binding as pending and request the restart that moves the unit.

        `juju bind` fires config-changed. Juju may still report the old address then, so
        update-status converges the unit too.
        """
        if (
            self.charm.state.substrate != Substrate.VM
            or not self.charm.state.unit_server.is_started
            or not self.charm.state.unit_server.model.private_ip
        ):
            # K8s uses hostnames, and the start flow records the address itself
            return

        try:
            bind_address = self.charm.state.bind_address
        except ValueError:
            # no address in the bound space, the unit stays on the recorded one
            bind_address = ""

        if not bind_address or bind_address == self.charm.state.unit_server.model.private_ip:
            if (
                self.charm.state.unit_server.model.pending_ip
                and self.charm.state.unit_server.model.pending_ip
                != self.charm.state.unit_server.model.private_ip
            ):
                logger.info("The binding has no new address anymore, dropping the move")
                self.charm.state.unit_server.update({"pending_ip": ""})
            return

        if self.charm.refresh_manager.refresh_in_progress:
            logger.info("Waiting for the refresh to finish before moving address")
            return

        if self.charm.state.unit_server.model.pending_ip != bind_address:
            logger.info("Moving to %s", bind_address)
            self.charm.state.unit_server.update({"pending_ip": bind_address})
            self.charm.request_restart()

    def restart_workload(
        self,
        restart_valkey: bool = True,
        restart_sentinel: bool = True,
        primary_endpoint: str = "",
    ) -> OperationResult:
        """Restart the workload once the rolling lock is granted to this unit.

        This is the rollingops `restart` callback.

        Args:
            restart_valkey: Whether to restart the Valkey service.
            restart_sentinel: Whether to restart the Sentinel service.
            primary_endpoint: Address of the primary. If set, Sentinel's config is rewritten
                right before the restart, because Sentinel rewrites that file on its own.

        Returns:
            RELEASE once the restarted services are healthy or if the start flow has not started
            the unit yet, RETRY_RELEASE during a backup or restore, RETRY_HOLD otherwise, so no
            other unit restarts or starts while this one is unhealthy. A pending address move
            replaces the plain restart, see `_move_address`.
        """
        logger.info(
            "Restarting workload. Restart Valkey: %s, Restart Sentinel: %s",
            restart_valkey,
            restart_sentinel,
        )

        if not self.charm.state.unit_server.is_started:
            logger.info("Unit not started yet, dropping the restart, the start applies the config")
            self.charm.state.unit_server.update({"restart_run_id": ""})
            return OperationResult.RELEASE

        if (
            self.charm.state.unit_server.is_backup_in_progress
            or self.charm.state.cluster.is_restore_in_progress
        ):
            logger.info("Backup/restore in progress on this unit; retrying the restart later")
            return OperationResult.RETRY_RELEASE

        if self.charm.state.unit_server.model.pending_ip:
            result = self._move_address()
        else:
            result = self._restart_services(restart_valkey, restart_sentinel, primary_endpoint)

        if result == OperationResult.RELEASE:
            # cleared only once healthy, so a retry doesn't restart Valkey again
            self.charm.state.unit_server.update({"restart_run_id": ""})
        return result

    def _restart_services(
        self, restart_valkey: bool, restart_sentinel: bool, primary_endpoint: str
    ) -> OperationResult:
        """Restart the services and check their health, see `restart_workload`."""
        try:
            # a RETRY_HOLD retry must not kill the load or sync the previous attempt started
            if restart_valkey and self.charm.cluster_manager.runs_requested_process():
                self.charm.workload.restart(self.charm.workload.valkey_service)
            if restart_sentinel:
                # if primary endpoint is given, write sentinel config
                # this is necessary as Sentinel may rewrite its config file since the last write
                if primary_endpoint != "":
                    self.charm.config_manager.set_sentinel_config_properties(
                        primary_endpoint=primary_endpoint
                    )
                self.charm.sentinel_manager.restart_service()
        except (
            ValkeyServicesFailedToStartError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error(e)
            return OperationResult.RETRY_HOLD

        if restart_valkey:
            if not self.charm.cluster_manager.is_back_in_sync():
                self.charm.state.unit_server.update({"is_valkey_healthy": False})
                return OperationResult.RETRY_HOLD
            # CONFIG SET min-replicas-to-write does not survive the restart we
            # just performed; the rendered file ships 1, so reassert the
            # topology-correct runtime value (0 on < 3 active units) now that
            # Valkey is back up and healthy, or a small cluster would be
            # write-frozen until the next peer-relation event.
            self.charm.cluster_manager.reconcile_min_replicas_to_write()
        self.charm.state.unit_server.update({"is_valkey_healthy": True})

        if restart_sentinel and not self.charm.sentinel_manager.is_healthy():
            self.charm.state.unit_server.update({"is_sentinel_healthy": False})
            return OperationResult.RETRY_HOLD

        self.charm.state.unit_server.update({"is_sentinel_healthy": True})
        return OperationResult.RELEASE

    def _move_address(self) -> OperationResult:
        """Move this unit to its `pending_ip` with at most one failover, see DA296.

        Returns:
            RETRY_RELEASE while the move waits for its turn, RETRY_HOLD until the moved unit
            is healthy and every Sentinel forgot the old address, RELEASE after.
        """
        if (
            self.charm.state.unit_server.model.private_ip
            != self.charm.state.unit_server.model.pending_ip
        ):
            if not self._ready_to_move():
                return OperationResult.RETRY_RELEASE

            try:
                run_id = self.charm.cluster_manager.run_id()
            except ValkeyWorkloadCommandError:
                # the old address is gone, as after a DHCP change
                run_id = VALKEY_DOWN_RUN_ID
            # the restart must replace the running process, whatever restarted it since
            self.charm.state.unit_server.update(
                {
                    "private_ip": self.charm.state.unit_server.model.pending_ip,
                    "restart_run_id": run_id,
                }
            )

        try:
            if (
                not self.charm.state.client_tls_relation
                and self.charm.tls_manager.certificate_sans_require_update()
            ):
                # Valkey serves the loaded certificate until the restart
                self.charm.tls_manager.create_and_store_self_signed_certificate()

            if self.charm.app.planned_units() == 1:
                # the unit stays primary, and its Sentinel no longer answers on the old address
                primary_endpoint = self.charm.state.endpoint
            else:
                primary_endpoint = self.charm.sentinel_manager.get_primary_ip()
            # rendered after any failover, whose CONFIG REWRITE replaces older renders
            self.charm.config_manager.set_config_properties(primary_endpoint=primary_endpoint)
        except (ValkeyCannotGetPrimaryIPError, ValkeyWorkloadCommandError) as e:
            logger.error("Failed to render the config for the move: %s", e)
            return OperationResult.RETRY_HOLD

        if (
            result := self._restart_services(True, True, primary_endpoint)
        ) != OperationResult.RELEASE:
            return result

        try:
            # Sentinel only forgets the old address on a reset. Reset before the next unit gets
            # the lock, because stale entries count toward the majority a failover needs.
            self.charm.sentinel_manager.reset_sentinel_states(
                self.charm.sentinel_manager.all_sentinel_endpoints()
            )
            self.charm.sentinel_manager.verify_expected_replica_count(
                self.charm.sentinel_manager.all_sentinel_endpoints()
            )
        except (
            CannotSeeAllActiveSentinelsError,
            SentinelIncorrectReplicaCountError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Sentinel reset after the move failed: %s", e)
            return OperationResult.RETRY_HOLD

        self.charm.state.unit_server.update({"pending_ip": ""})
        logger.info("Moved to %s", self.charm.state.endpoint)
        if self.charm.state.client_tls_relation:
            # the provider's certificate still lists the old address
            self.charm.on.refresh_tls_certificates.emit()
        return OperationResult.RELEASE

    def _ready_to_move(self) -> bool:
        """Check the move can start, and hand over the primary once every other unit moved.

        Sentinel decides who is primary, as before a refresh, so an unreachable primary
        waits for its failover.

        Returns:
            bool: False if the move must wait.
        """
        if self.charm.refresh_manager.refresh_in_progress:
            logger.info("Waiting for the refresh to finish before moving address")
            return False

        try:
            if self.charm.state.client_tls_relation and (
                self.charm.state.unit_server.model.pending_ip
                not in self.charm.tls_manager.get_current_sans()["sans_ip"]
            ):
                logger.info("Waiting for a certificate valid for the new address")
                return False

            if (
                self.charm.app.planned_units() == 1
                or self.charm.sentinel_manager.get_primary_ip()
                != self.charm.state.unit_server.model.private_ip
            ):
                return True
        except (ValkeyCannotGetPrimaryIPError, ValkeyWorkloadCommandError) as e:
            logger.error("Cannot start the move yet: %s", e)
            return False

        if self.charm.state.other_units_moving:
            logger.info(
                "The primary moves last, waiting for %s", self.charm.state.other_units_moving
            )
            return False

        try:
            self.charm.sentinel_manager.failover()
        except SentinelFailoverError as e:
            logger.error("Failover before the move failed: %s", e)
            return False
        return True

    def _on_refresh_peer_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Start a move that waited for the refresh.

        charm_refresh writes to every unit's databag once the refresh completes.
        """
        self._request_address_move()

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Handle the config_changed event."""
        self._request_address_move()

        if not self.charm.unit.is_leader():
            return

        self._store_persistence_config()

        # the leader gets no relation-changed for its own app databag write
        state = self.charm.state
        if state.unit_server.is_started and not state.cluster.is_restore_in_progress:
            self.charm.reconcile_persistence()

        if admin_secret_id := self.charm.config.get(INTERNAL_USERS_PASSWORD_CONFIG):
            try:
                self._update_internal_users_password(str(admin_secret_id))
            except (
                ops.ModelError,
                ops.SecretNotFoundError,
                ValkeyACLLoadError,
                ValkeyWorkloadCommandError,
            ):
                event.defer()
                return

            # propagate updated credentials to topology observer
            try:
                self.charm.topology_manager.restart_observer()
            except (ValkeyWorkloadCommandError, ValueError) as e:
                logger.error("Failed to restart topology observer: %s", e)

    def _store_persistence_config(self) -> None:
        """Store valid persistence settings for all units, keep the last valid ones otherwise."""
        try:
            persistence = PersistenceConfig.from_charm_config(self.charm.config)
        except ValidationError as e:
            logger.error("Invalid persistence config, keeping the last valid settings: %s", e)
            return

        self.charm.state.cluster.update({"persistence_config": persistence})

    def _on_secret_changed(self, event: ops.SecretChangedEvent) -> None:
        """Handle the secret_changed event."""
        if not (admin_secret_id := self.charm.config.get(INTERNAL_USERS_PASSWORD_CONFIG)):
            return

        if self.charm.unit.is_leader():
            if admin_secret_id == event.secret.id:
                try:
                    self._update_internal_users_password(str(admin_secret_id))
                except (
                    ops.ModelError,
                    ops.SecretNotFoundError,
                    ValkeyACLLoadError,
                    ValkeyWorkloadCommandError,
                ):
                    event.defer()
                    return

                # propagate updated credentials to topology observer
                try:
                    self.charm.topology_manager.restart_observer()
                except (ValkeyWorkloadCommandError, ValueError) as e:
                    logger.error("Failed to restart topology observer: %s", e)

            return

        # from here, code is only relevant for non-leader units
        if event.secret.label and event.secret.label.endswith(INTERNAL_USERS_SECRET_LABEL_SUFFIX):
            # leader unit processed the secret change from user, non-leader units can replicate
            try:
                self.charm.auth_manager.set_acl_file()
                self.charm.auth_manager.set_sentinel_acl_file()
                if self.charm.state.unit_server.is_started:
                    self.charm.cluster_manager.reload_acl_file()
                    self.charm.request_restart(restart_valkey=False, restart_sentinel=True)
                # update the local unit admin password to match the leader
                self.charm.auth_manager.update_local_valkey_admin_password()
                if self.charm.state.unit_server.is_started:
                    self.charm.cluster_manager.update_primary_auth()
            except (ops.ModelError, ValkeyACLLoadError, ValkeyWorkloadCommandError) as e:
                logger.error(e)
                self.charm.status.set_running_status(
                    ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                    scope="unit",
                    component_name=self.charm.cluster_manager.name,
                    statuses_state=self.charm.state.statuses,
                )
                event.defer()
                return
            self.charm.state.statuses.delete(
                ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                scope="unit",
                component=self.charm.cluster_manager.name,
            )

    def _update_internal_users_password(self, secret_id: str) -> None:
        """Update internal users' passwords in charm/valkey if they have changed.

        Args:
            secret_id (str): The id of the secret containing the internal users' passwords.
        """
        try:
            secret_content = self.charm.state.get_secret_from_id(secret_id)
        except (ops.ModelError, ops.SecretNotFoundError) as e:
            logger.error(e)
            self.charm.status.set_running_status(
                CharmStatuses.SECRET_ACCESS_ERROR.value,
                scope="app",
                component_name=self.charm.cluster_manager.name,
                statuses_state=self.charm.state.statuses,
            )
            raise

        self.charm.state.statuses.delete(
            CharmStatuses.SECRET_ACCESS_ERROR.value,
            scope="app",
            component=self.charm.cluster_manager.name,
        )

        if any(key not in CharmUsers for key in secret_content.keys()):
            logger.error("Invalid username in secret %s.", secret_id)
            self.charm.status.set_running_status(
                ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                scope="app",
                component_name=self.charm.cluster_manager.name,
                statuses_state=self.charm.state.statuses,
            )
            # do not raise here, we don't want to run again if data is wrong
            return

        # merge the credentials, replacing those which have been updated
        new_passwords = self.charm.state.cluster.internal_users_credentials | secret_content
        if new_passwords != self.charm.state.cluster.internal_users_credentials:
            logger.info("Password(s) for internal users have changed")
            try:
                self.charm.auth_manager.set_acl_file(passwords=new_passwords)
                self.charm.auth_manager.set_sentinel_acl_file(passwords=new_passwords)
                if self.charm.state.unit_server.is_started:
                    self.charm.cluster_manager.reload_acl_file()
                    self.charm.request_restart(restart_valkey=False, restart_sentinel=True)
                self.charm.state.cluster.update(
                    {
                        f"{user.value.replace('-', '_')}_password": new_passwords[user.value]
                        for user in CharmUsers
                    }
                )
                # update the local unit admin password
                self.charm.auth_manager.update_local_valkey_admin_password()
                if self.charm.state.unit_server.is_started:
                    self.charm.cluster_manager.update_primary_auth()
                self.charm.metrics_manager.reconcile()
            except (
                ValkeyACLLoadError,
                ValueError,
                ValkeyWorkloadCommandError,
            ) as e:
                logger.error(e)
                self.charm.status.set_running_status(
                    ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
                    scope="unit",
                    component_name=self.charm.cluster_manager.name,
                    statuses_state=self.charm.state.statuses,
                )
                raise e

        self.charm.state.statuses.delete(
            ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )
        self.charm.state.statuses.delete(
            ClusterStatuses.PASSWORD_UPDATE_FAILED.value,
            scope="app",
            component=self.charm.cluster_manager.name,
        )

    def _on_storage_detaching(self, event: ops.StorageDetachingEvent) -> None:
        """Handle removal of a storage mount, e.g. when removing a unit.

        Unit teardown detaches every storage; whichever detaches first runs the
        safe scale-down (which stops the workload), so the later ones are no-ops.
        """
        if self.charm.state.unit_server.is_being_removed:
            return

        if (
            self.charm.state.unit_server.is_backup_in_progress
            or self.charm.state.cluster.is_restore_in_progress
        ):
            # Raise (not return) so the hook errors and Juju retries until the
            # backup/restore finishes; a plain return would lose the in-flight RDB.
            raise ValkeyBackupInProgressError(
                "Backup or restore in progress on this unit; refusing to scale down until it finishes."
            )

        self._scale_down_unit()

    def _scale_down_unit(self) -> None:
        """Failover if needed, flush the dataset, and stop the workload."""
        self.charm.status.set_running_status(
            ScaleDownStatuses.WAIT_FOR_LOCK.value,
            scope="unit",
            component_name=self.charm.cluster_manager.name,
            statuses_state=self.charm.state.statuses,
        )

        # without a primary there is nothing to coordinate with
        try:
            primary_ip = self.charm.sentinel_manager.get_primary_ip_for_scale_down()
        except ValkeyCannotGetPrimaryIPError as e:
            logger.error(e)
            self._leave_without_coordination()
            return

        # Sentinel keeps reporting a primary that is down, and the lock lives on the primary
        if not self.charm.cluster_manager.is_healthy(
            is_primary=True, check_replica_sync=False, hostname=primary_ip
        ):
            logger.error("Primary %s is unreachable, scaling down without the lock", primary_ip)
            self._leave_without_coordination()
            return

        # blocks until the lock is acquired, a timeout raises and Juju retries the hook
        with self.charm.rollingops.acquire_sync_lock(
            SCALE_DOWN_LOCK_ID, timeout=SCALE_DOWN_LOCK_TIMEOUT_S
        ):
            self._scale_down_holding_lock()

    def _scale_down_holding_lock(self) -> None:
        """Failover if needed, flush the dataset, stop the workload and mark the unit going away.

        The lock backend releases the lock after the unit is marked going away.
        """
        self.charm.state.statuses.delete(
            ScaleDownStatuses.WAIT_FOR_LOCK.value,
            scope="unit",
            component=self.charm.cluster_manager.name,
        )

        self.charm.status.set_running_status(
            ScaleDownStatuses.SCALING_DOWN.value,
            scope="unit",
            component_name=self.charm.cluster_manager.name,
            statuses_state=self.charm.state.statuses,
        )

        # if unit has primary then failover
        try:
            primary_ip = self.charm.sentinel_manager.get_primary_ip_for_scale_down()
        except ValkeyCannotGetPrimaryIPError as e:
            logger.error(e)
            self._leave_without_coordination()
            return

        active_sentinels = self.charm.sentinel_manager.get_active_sentinel_ips(primary_ip)
        unit_is_primary = primary_ip == self.charm.state.unit_server.get_endpoint(
            self.charm.state.substrate
        )

        if unit_is_primary and len(active_sentinels) > 1:
            logger.debug("Triggering sentinel failover on primary IP %s", primary_ip)
            self.charm.sentinel_manager.failover()
            primary_ip = self.charm.sentinel_manager.get_primary_ip()
            logger.debug(
                "Failover completed, new primary ip %s",
                primary_ip,
            )

        if self.charm.unit.is_leader():
            self.charm.topology_manager.stop_observer()

        if not unit_is_primary:
            logger.info("Waiting for replica to be fully-synced before saving the dataset")
            self.charm.cluster_manager.wait_for_replica_fully_synced(primary_ip)

        # Raises if the save fails, so the hook errors and Juju retries it with the workload still up.
        self.charm.cluster_manager.save_dataset_before_shutdown()
        self._stop_workload()

        self._set_state_for_going_away()

    def _leave_without_coordination(self) -> None:
        """Save the dataset if the local Valkey answers, then stop and mark the unit going away."""
        # a dead local Valkey has nothing to save, and must not block the removal
        if self.charm.cluster_manager.is_healthy(
            is_primary=True, check_replica_sync=False
        ) or self.charm.workload.alive(service=self.charm.workload.valkey_service):
            self.charm.cluster_manager.save_dataset_before_shutdown()
        self._stop_workload()
        self._set_state_for_going_away()

    def _stop_workload(self) -> None:
        """Stop the services and reset the start state."""
        try:
            self.charm.workload.stop()
        except ValkeyServicesCouldNotBeStoppedError as e:
            logger.error("Could not stop Valkey services cleanly: %s", e)

        self.charm.state.unit_server.update({"start_state": StartState.NOT_STARTED.value})

    def _set_state_for_going_away(self) -> None:
        """Set the state to going away when the unit is going down."""
        if self.charm.app.planned_units() == 0 and self.charm.unit.is_leader():
            # clear app data bag
            self.charm.state.cluster.update(
                {
                    "internal_ca_certificate": None,
                    "internal_ca_private_key": None,
                }
            )

        self.charm.state.unit_server.update({"scale_down_state": ScaleDownState.GOING_AWAY.value})

    def _reconfigure_quorum_if_necessary(self) -> None:
        """Reconfigure the sentinel quorum if it does not match the current cluster size."""
        # if the unit / all units are being removed, we do not need to reconfigure the quorum
        if (
            not self.charm.state.unit_server.is_active
            or self.charm.state.unit_server.is_being_removed
            or self.model.app.planned_units() == 0
            # to avoid failures if a Sentinel has not been restarted yet
            # does not rely on TLS state because databag might be outdated in deferred events
            or (
                self.charm.state.client_tls_relation
                and not self.charm.state.unit_server.is_tls_enabled
            )
            or (
                self.charm.state.unit_server.is_tls_enabled
                and not self.charm.state.client_tls_relation
            )
        ):
            return

        if self.charm.sentinel_manager.get_configured_quorum() != self.charm.config_manager.quorum:
            logger.debug("Updating sentinel quorum to match current cluster size")
            self.charm.sentinel_manager.set_quorum(self.charm.config_manager.quorum)
            self.charm.config_manager.set_sentinel_config_properties(
                self.charm.sentinel_manager.get_primary_ip()
            )
