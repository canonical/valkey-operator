#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all cluster related tasks."""

import logging
from collections.abc import Callable
from time import sleep

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope
from ops import pebble
from tenacity import (
    Retrying,
    retry,
    retry_if_not_exception_type,
    retry_if_result,
    stop_after_attempt,
    stop_after_delay,
    wait_fixed,
)

from common.client import ValkeyClient
from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyClusterNotReadyError,
    ValkeyServerRestartedError,
    ValkeyTLSLoadError,
    ValkeyWorkloadCommandError,
)
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import (
    AOF_DIR_NAME,
    AOF_LOAD_FATAL_LOG_LINES,
    AOF_MANIFEST_NAME,
    DATASET_IO_TIMEOUT_S,
    VALKEY_LOG_FILE,
    VALKEY_LOG_TAIL_LINES,
    VALKEY_READY_LOG_LINE,
    VALKEY_START_LOG_LINE,
    CharmUsers,
    ScaleDownState,
    StartState,
)
from statuses import CharmStatuses, ClusterStatuses, ScaleDownStatuses, StartStatuses

logger = logging.getLogger(__name__)


class ClusterManager(ManagerStatusProtocol):
    """Manage cluster members, authorization and other server related tasks."""

    name: str = "cluster"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        # `ClusterState` satisfies `StatusesStateProtocol`; pyright flags this only
        # because the protocol declares `state` as a mutable (invariant) attribute.
        self.state = state  # pyright: ignore[reportIncompatibleVariableOverride]
        self.workload = workload
        self.admin_user = CharmUsers.VALKEY_ADMIN.value

    @property
    def admin_password(self) -> str:
        """Get the password of the admin user for the Valkey cluster."""
        return self.state.unit_server.valkey_admin_password

    def _get_valkey_client(self) -> ValkeyClient:
        """Get a client connection to Valkey."""
        return ValkeyClient(
            username=self.admin_user,
            password=self.admin_password,
            tls=self.state.unit_server.is_tls_enabled,
            workload=self.workload,
        )

    def reload_acl_file(self) -> None:
        """Reload the ACL file into the cluster."""
        client = self._get_valkey_client()
        if not client.acl_load(hostname=self.state.endpoint):
            raise ValkeyACLLoadError("Could not load ACL file into Valkey cluster.")

    def reconcile_min_replicas_to_write(self) -> None:
        """Set min-replicas-to-write at runtime to match the current topology.

        Enabled (1) only on clusters that can currently tolerate losing a
        replica (>= 3 active units); disabled (0) otherwise so the primary is
        not write-frozen when a replica is unavailable (e.g. during a rolling
        restart, a scale-down, or a scale-up that is still rolling out).
        Counting active units rather than planned units avoids freezing writes
        when the planned count has already grown but the new units are not up
        yet. The rendered config ships 1, so the relaxed (0) value must be
        reasserted after every (re)start because CONFIG SET does not persist.

        A failed CONFIG SET is logged and swallowed rather than raised: the
        value is non-critical and gets reasserted on the next event or restart.
        """
        active_units = len([server for server in self.state.servers if server.is_active])
        value = "1" if active_units >= 3 else "0"
        try:
            client = self._get_valkey_client()
            client.config_set(
                hostname=self.state.endpoint,
                config_settings={
                    "min-replicas-to-write": value,
                },
            )
        except ValkeyWorkloadCommandError as e:
            # non-critical; reasserted on the next event or restart
            logger.error("Failed to reconcile min-replicas-to-write: %s", e)

    def update_primary_auth(self) -> None:
        """Update the primaryauth runtime configuration on the Valkey server."""
        client = self._get_valkey_client()
        client.config_set(
            hostname=self.state.endpoint,
            config_settings={
                "primaryauth": self.state.cluster.internal_users_credentials.get(
                    CharmUsers.VALKEY_REPLICA.value, ""
                )
            },
        )

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_replica_synced(self, hostname: str | None = None) -> bool:
        """Check if the replica is synced with the primary."""
        client = self._get_valkey_client()
        if not hostname:
            hostname = self.state.endpoint

        try:
            role_info = client.role(hostname)
        except ValkeyWorkloadCommandError as e:
            logger.warning("Could not get the role information: %s", e)
            return False

        try:
            return role_info[0] == "slave" and role_info[3] == "connected"
        except IndexError as e:
            logger.warning("Unexpected role information format: %s. Error: %s", role_info, e)
            return False

    def is_primary(self) -> bool:
        """Return True if this unit's Valkey server currently reports the primary role."""
        client = self._get_valkey_client()
        return client.role(hostname=self.state.endpoint)[0] == "master"

    def get_replication_offset(self, primary_endpoint: str | None = None) -> int:
        """Query the current replication offset from Valkey.

        Args:
            primary_endpoint: If given, return the primary replication offset from this primary,
                            otherwise get the replica's replication offset from the current unit.
        """
        client = self._get_valkey_client()

        if primary_endpoint:
            role_info = client.role(hostname=primary_endpoint)
            try:
                return int(role_info[1])
            except (IndexError, TypeError, ValueError) as e:
                logger.error("Failed to query replication offset from primary: %s", e)
                raise ValkeyWorkloadCommandError

        role_info = client.role(hostname=self.state.endpoint)
        try:
            return int(role_info[4])
        except (IndexError, TypeError, ValueError) as e:
            logger.error("Failed to query replication offset from replica: %s", e)
            raise ValkeyWorkloadCommandError

    def wait_for_replica_fully_synced(self, primary_endpoint: str):
        """Compare the unit's replication offset with the primary's offset and block until synced."""
        try:
            primary_replication_offset = self.get_replication_offset(primary_endpoint)
            while self.get_replication_offset() < primary_replication_offset:
                logger.info("Replica not fully synced yet")
                sleep(5)
            logger.info("Replica is fully synced now")
        except ValkeyWorkloadCommandError:
            logger.error("Could not query replication offset")
            return

    def is_loaded(self) -> bool:
        """Whether the local server responds and has finished loading its dataset."""
        client = self._get_valkey_client()
        return (
            client.ping(hostname=self.state.endpoint)
            and client.info_persistence(hostname=self.state.endpoint).get("loading", "1") == "0"
        )

    def wait_until_loaded(self, timeout_s: int) -> None:
        """Bounded poll until the server responds and has finished loading.

        Raises ValkeyClusterNotReadyError on timeout rather than hanging, so the
        caller can react (e.g. roll back a restore).
        """
        logger.info("restore.wait: dataset to load (up to %ss)", timeout_s)
        self._wait_until(self.is_loaded, timeout_s, "server did not become ready")
        logger.info("restore.wait: dataset loaded")

    def wait_until_resynced(self, timeout_s: int) -> None:
        """Bounded poll until this replica is in sync with the primary.

        Unlike wait_for_replica_fully_synced (unbounded), raises
        ValkeyClusterNotReadyError on timeout so the caller can react.
        """
        logger.info("restore.wait: replica resync (up to %ss)", timeout_s)
        self._wait_until(self.is_replica_synced, timeout_s, "replica did not resync")
        logger.info("restore.wait: replica resynced")

    def _wait_until(
        self,
        condition: Callable[[], bool],
        timeout_s: int,
        failure: str,
        run_id: str | None = None,
    ) -> None:
        """Poll ``condition`` every 5s for up to ``timeout_s``; raise NotReady on timeout.

        A raising ``condition`` (e.g. the CLI can't connect yet) is retried like
        a False one; the last error is chained onto the timeout exception. With
        ``run_id``, the wait also fails on the first poll after a Valkey restart.
        A restart kills the rewrite or BGSAVE being waited on, and the new process
        reports idle defaults, so waiting longer can't help.

        Raises:
            ValkeyServerRestartedError: If Valkey restarted since ``run_id`` was read.
            ValkeyClusterNotReadyError: If ``condition`` is not met within ``timeout_s``.
        """
        try:
            for attempt in Retrying(
                stop=stop_after_delay(timeout_s),
                wait=wait_fixed(5),
                retry=retry_if_not_exception_type(ValkeyServerRestartedError),
                reraise=True,
            ):
                with attempt:
                    met = condition()
                    # check after the condition, so a restart before or during that read fails here
                    if run_id:
                        self._check_not_restarted(run_id)
                    if not met:
                        raise ValkeyClusterNotReadyError("condition not met yet")
        except ValkeyServerRestartedError:
            raise
        except Exception as e:  # tenacity reraises the last attempt's error
            raise ValkeyClusterNotReadyError(f"{failure} within {timeout_s}s") from e

    def run_id(self) -> str:
        """Return the ID of the running Valkey process, which changes on every restart.

        Raises:
            ValkeyWorkloadCommandError: If the server can't be queried or the reply has no run_id.
        """
        info = self._get_valkey_client().info_server(hostname=self.state.endpoint)
        if not (run_id := info.get("run_id")):
            raise ValkeyWorkloadCommandError("INFO server reply has no run_id")
        return run_id

    def _check_not_restarted(self, run_id: str) -> None:
        """Raise if Valkey restarted since the caller read ``run_id``.

        Raises:
            ValkeyServerRestartedError: If the process ID changed.
            ValkeyWorkloadCommandError: If the server can't be queried.
        """
        if self.run_id() != run_id:
            raise ValkeyServerRestartedError("Valkey restarted during a persistence switch")

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_healthy(
        self,
        is_primary: bool = False,
        check_replica_sync: bool = True,
        hostname: str | None = None,
    ) -> bool:
        """Check if a valkey instance is healthy."""
        client = self._get_valkey_client()
        if not hostname:
            hostname = self.state.endpoint

        if not client.ping(hostname):
            logger.warning("Health check failed: Valkey server did not respond to ping.")
            return False

        try:
            persistence_info = client.info_persistence(hostname)
        except ValkeyWorkloadCommandError as e:
            logger.error(e)
            return False

        if persistence_info.get("loading", "") != "0":
            logger.warning("Health check failed: Valkey server is still loading data.")
            return False

        if not is_primary and check_replica_sync and not self.is_replica_synced(hostname):
            logger.warning("Health check failed: Replica is not synced with primary.")
            return False

        return True

    def all_servers_healthy(self, primary_endpoint: str) -> bool:
        """Check if all valkey servers are healthy."""
        all_servers = [
            unit.get_endpoint(self.state.substrate)
            for unit in self.state.servers
            if unit.is_active
        ]

        for server in all_servers:
            if not self.is_healthy(is_primary=server == primary_endpoint, hostname=server):
                return False

        return True

    def get_version(self, primary_endpoint: str) -> str:
        """Get the Valkey version from the primary server."""
        client = self._get_valkey_client()
        try:
            server_info = client.info_server(hostname=primary_endpoint)
        except ValkeyWorkloadCommandError as e:
            logger.warning("Can't get server info: %s", e)
            return ""

        return server_info["valkey_version"]

    def reload_tls_settings(self, tls_config: dict[str, str]) -> None:
        """Update TLS by loading the TLS settings."""
        client = self._get_valkey_client()
        try:
            client.config_set(tls_config, hostname=self.state.endpoint)
        except ValkeyWorkloadCommandError:
            logger.error("Error loading TLS settings")
            raise ValkeyTLSLoadError("Could not load TLS settings")

    def reload_ldap_settings(self, ldap_config: dict[str, str]) -> None:
        """Update Valkey auth by loading the LDAP settings."""
        if not ldap_config:
            # disable LDAP by unsetting the server connection
            ldap_config = {"ldap.servers": ""}

        client = self._get_valkey_client()
        client.config_set(ldap_config, hostname=self.state.endpoint)

    def _is_bgsave_in_progress(self) -> bool:
        """Check for a running background save, which makes SAVE fail.

        Returns:
            True if a BGSAVE is in progress or the state cannot be read from the reply.
        """
        client = self._get_valkey_client()
        persistence = client.info_persistence(hostname=self.state.endpoint)
        return persistence.get("rdb_bgsave_in_progress", "1") != "0"

    def _save_database_blocking(self) -> None:
        """Wait out any running BGSAVE, then run a blocking save.

        Raises:
            ValkeyClusterNotReadyError: If a BGSAVE is still running after ``DATASET_IO_TIMEOUT_S``.
            ValkeyWorkloadCommandError: If the save fails.
        """
        self._wait_until(
            lambda: not self._is_bgsave_in_progress(),
            DATASET_IO_TIMEOUT_S,
            "background save did not finish",
        )
        client = self._get_valkey_client()
        client.save(hostname=self.state.endpoint)

    def _is_aof_rewrite_idle(self) -> bool:
        """Whether no AOF rewrite is running or scheduled."""
        info = self._get_valkey_client().info_persistence(hostname=self.state.endpoint)
        return (
            info.get("aof_rewrite_in_progress") == "0" and info.get("aof_rewrite_scheduled") == "0"
        )

    def _is_aof_on(self) -> bool:
        """Whether AOF is on and its first rewrite finished, so a complete AOF is on disk."""
        info = self._get_valkey_client().info_persistence(hostname=self.state.endpoint)
        return (
            info.get("aof_enabled") == "1"
            and info.get("aof_rewrite_in_progress") == "0"
            and info.get("aof_rewrite_scheduled") == "0"
        )

    def _save_in_background(self, run_id: str) -> None:
        """Run a BGSAVE and wait for it, without blocking clients like SAVE does.

        Args:
            run_id: The Valkey process ID the switch started on.

        Raises:
            ValkeyServerRestartedError: If Valkey restarts before the BGSAVE is confirmed.
            ValkeyClusterNotReadyError: If a child process or the BGSAVE does not finish in time.
            ValkeyWorkloadCommandError: If the BGSAVE fails.
        """
        self._wait_until(
            lambda: self._is_aof_rewrite_idle() and not self._is_bgsave_in_progress(),
            DATASET_IO_TIMEOUT_S,
            "a background child process did not finish",
            run_id=run_id,
        )
        client = self._get_valkey_client()
        client.bgsave(hostname=self.state.endpoint)
        self._wait_until(
            lambda: not self._is_bgsave_in_progress(),
            DATASET_IO_TIMEOUT_S,
            "BGSAVE did not finish",
            run_id=run_id,
        )
        info = client.info_persistence(hostname=self.state.endpoint)
        # a restarted server reports ok whatever happened to our BGSAVE
        self._check_not_restarted(run_id)
        if info.get("rdb_last_bgsave_status") != "ok":
            raise ValkeyWorkloadCommandError("BGSAVE failed")

    def enable_new_persistence(self, settings: dict[str, str]) -> None:
        """Turn on, without a restart, the persistence the new settings add.

        The old settings stay on, so the persistence the current valkey.conf names stays current.

        Args:
            settings: The new Valkey persistence keys from ``ConfigManager.persistence_settings``.

        Raises:
            ValkeyServerRestartedError: If Valkey restarts during an AOF rewrite or a BGSAVE.
            ValkeyClusterNotReadyError: If an AOF rewrite or a BGSAVE does not finish in time.
            ValkeyWorkloadCommandError: If a command fails.
        """
        client = self._get_valkey_client()
        # a restart reloads valkey.conf and loses the CONFIG SET changes below
        run_id = self.run_id()
        if settings["appendonly"] == "yes":
            client.config_set({"appendonly": "yes"}, hostname=self.state.endpoint)
            self._wait_until(
                self._is_aof_on, DATASET_IO_TIMEOUT_S, "AOF rewrite did not finish", run_id=run_id
            )

        rdb_on = {"save": settings["save"]} if settings["save"] else {}
        client.config_set(
            {
                "appendfsync": settings["appendfsync"],
                "shutdown-on-sigterm": settings["shutdown-on-sigterm"],
                **rdb_on,
            },
            hostname=self.state.endpoint,
        )

        # once the file says appendonly no, a restart loads dump.rdb, so make it current first
        aof_on = client.info_persistence(hostname=self.state.endpoint).get("aof_enabled") == "1"
        if settings["appendonly"] == "no" and settings["save"] and aof_on:
            self._save_in_background(run_id)

    def disable_old_persistence(self, settings: dict[str, str]) -> None:
        """Turn off, without a restart, the old persistence the new settings drop.

        Call it only once valkey.conf names the new settings, so a restart loads current data.

        Args:
            settings: The new Valkey persistence keys from ``ConfigManager.persistence_settings``.

        Raises:
            ValkeyWorkloadCommandError: If the server refuses the settings.
        """
        off = {"rdb-del-sync-files": settings["rdb-del-sync-files"]}
        if not settings["save"]:
            off["save"] = ""
        if settings["appendonly"] == "no":
            off["appendonly"] = "no"
        self._get_valkey_client().config_set(off, hostname=self.state.endpoint)

    def _disable_save_on_shutdown(self) -> None:
        """Set the configured shutdown option to 'no save' temporarily, will not persist a restart."""
        client = self._get_valkey_client()
        client.config_set(
            hostname=self.state.endpoint,
            config_settings={"shutdown-on-sigterm": "nosave"},
        )

    def is_live_persistence(self, settings: dict[str, str]) -> bool:
        """Whether the running server uses these persistence settings.

        Raises:
            ValkeyWorkloadCommandError: If the settings can't be read.
        """
        live = self._get_valkey_client().config_get(list(settings), self.state.endpoint)
        return live == settings

    def save_dataset_before_shutdown(self) -> None:
        """Save, then disable save-on-shutdown so Pebble cannot kill a shutdown save.

        Nothing is changed before the save succeeds, so callers must not stop the workload on
        failure and a retry is safe.

        Raises:
            ValkeyClusterNotReadyError: If an AOF rewrite or a BGSAVE is still running after
                ``DATASET_IO_TIMEOUT_S``.
            ValkeyWorkloadCommandError: If the save fails.
        """
        logger.info("Save dataset to disk")
        # DA290: never stop Valkey mid-rewrite, the fields read 0 when AOF is off
        self._wait_until(
            self._is_aof_rewrite_idle, DATASET_IO_TIMEOUT_S, "AOF rewrite did not finish"
        )
        self._save_database_blocking()

        try:
            self._disable_save_on_shutdown()
        except ValkeyWorkloadCommandError as e:
            logger.warning("Could not disable save on shutdown: %s", e)

    def clean_up_inconsistent_dump_files(self) -> None:
        """Find and clean up dump files that where not correctly stored.

        When the database is saved to disk on shutdown and this save is interrupted, the created
        dump file is inconsistent and must be removed by the operator code, because Valkey will
        not clean up on its own.
        """
        try:
            for temp_file in self.workload.working_dir.glob("temp-*.rdb"):
                logger.info("Removing temporary dump-file %s", temp_file)
                temp_file.unlink(missing_ok=True)
        except pebble.ConnectionError as e:
            logger.error("Error cleaning up inconsistent dump files: %s", e)
            raise ValkeyWorkloadCommandError("Error cleaning up inconsistent dump files") from e

    def is_aof_corrupted(self) -> bool:
        """Whether the local server is down because its last start failed to load the AOF.

        A LOADING reply means Valkey has not decided yet, so only a silent server gets checked.
        """
        if self._get_valkey_client().is_responding(self.state.endpoint):
            return False
        manifest = self.workload.working_dir / AOF_DIR_NAME / AOF_MANIFEST_NAME
        log_file = self.workload.log_dir / VALKEY_LOG_FILE
        if not self.workload.path_exists(manifest) or not self.workload.path_exists(log_file):
            return False
        # TODO: go back to valkey-check-aof once a Valkey 9.0 release with valkey-io/valkey#4739
        # reaches the Ubuntu archive. Until then it rejects every healthy AOF with an RDB base.
        log_tail = self.workload.read_file_tail(log_file, VALKEY_LOG_TAIL_LINES)
        last_start = log_tail.rpartition(VALKEY_START_LOG_LINE)[2]
        return VALKEY_READY_LOG_LINE not in last_start and any(
            line in last_start for line in AOF_LOAD_FATAL_LOG_LINES
        )

    def delete_corrupted_aof(self) -> None:
        """Delete the AOF dir so the server can start and full-sync from the primary.

        Raises:
            ValkeyWorkloadCommandError: If the delete fails.
        """
        aof_dir = self.workload.working_dir / AOF_DIR_NAME
        # without the manifest Valkey ignores leftover files if the rest fails partway
        self.workload.remove_file(aof_dir / AOF_MANIFEST_NAME)
        self.workload.remove_dir(aof_dir)
        logger.warning("Deleted the corrupted AOF in %s", aof_dir)

    def reset_client_connections(self) -> None:
        """Disconnect all clients to re-establish connections after failover."""
        endpoints = [
            unit.get_endpoint(self.state.substrate)
            for unit in self.state.servers
            # reset replica AND primary clients (for clients specifically connecting to a replica)
            if unit.is_active
        ]

        client = self._get_valkey_client()

        for endpoint in endpoints:
            logger.info("Reset client connections on server %s", endpoint)
            try:
                client.reset_client_connections(endpoint)
            except ValkeyWorkloadCommandError as e:
                logger.warning("Could not reset client connections on %s: %s", endpoint, e)

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the cluster manager's statuses."""
        status_list: list[StatusObject] = self.state.statuses.get(
            scope=scope,
            component=self.name,
            running_status_only=True,
        ).root

        # Peer relation not established yet, or model not built yet for unit or app
        if not self.state.cluster.model or not self.state.unit_server.model:
            return status_list or [CharmStatuses.ACTIVE_IDLE.value]

        if scope == "unit":
            if start_status := self._get_start_status():
                status_list.append(start_status)

            if scale_down_status := self._get_scale_down_status():
                status_list.append(scale_down_status)

            if not self.state.unit_server.model.is_valkey_healthy:
                status_list.append(ClusterStatuses.VALKEY_UNHEALTHY_RESTART.value)

            if not self.state.unit_server.model.is_sentinel_healthy:
                status_list.append(ClusterStatuses.SENTINEL_UNHEALTHY_RESTART.value)

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
        if self.state.unit_server.model.scale_down_state == ScaleDownState.WAIT_FOR_LOCK.value:
            return ScaleDownStatuses.GOING_AWAY.value

        return None
