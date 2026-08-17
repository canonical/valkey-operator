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
from tenacity import (
    Retrying,
    retry,
    retry_if_result,
    stop_after_attempt,
    stop_after_delay,
    wait_fixed,
)

from common.client import ValkeyClient
from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyClusterNotReadyError,
    ValkeyTLSLoadError,
    ValkeyWorkloadCommandError,
)
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import CharmUsers, ScaleDownState, StartState
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
    def is_replica_synced(self) -> bool:
        """Check if the replica is synced with the primary."""
        client = self._get_valkey_client()
        role_info = client.role(hostname=self.state.endpoint)
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
        self._wait_until(self.is_loaded, timeout_s, "server did not become ready")

    def wait_until_resynced(self, timeout_s: int) -> None:
        """Bounded poll until this replica is in sync with the primary.

        Unlike wait_for_replica_fully_synced (unbounded), raises
        ValkeyClusterNotReadyError on timeout so the caller can react.
        """
        self._wait_until(self.is_replica_synced, timeout_s, "replica did not resync")

    def _wait_until(self, condition: Callable[[], bool], timeout_s: int, failure: str) -> None:
        """Poll ``condition`` every 5s for up to ``timeout_s``; raise NotReady on timeout.

        A raising ``condition`` (e.g. the CLI can't connect yet) is retried like
        a False one; the last error is chained onto the timeout exception.
        """
        try:
            for attempt in Retrying(
                stop=stop_after_delay(timeout_s), wait=wait_fixed(5), reraise=True
            ):
                with attempt:
                    if not condition():
                        raise ValkeyClusterNotReadyError("condition not met yet")
        except Exception as e:  # tenacity reraises the last attempt's error
            raise ValkeyClusterNotReadyError(f"{failure} within {timeout_s}s") from e

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_attempt(5),
        retry=retry_if_result(lambda result: result is False),
        retry_error_callback=lambda _: False,
    )
    def is_healthy(self, is_primary: bool = False, check_replica_sync: bool = True) -> bool:
        """Check if a valkey instance is healthy."""
        client = self._get_valkey_client()

        if not client.ping(hostname=self.state.endpoint):
            logger.warning("Health check failed: Valkey server did not respond to ping.")
            return False

        try:
            persistence_info = client.info_persistence(hostname=self.state.endpoint)
        except ValkeyWorkloadCommandError as e:
            logger.error(e)
            return False

        if persistence_info.get("loading", "") != "0":
            logger.warning("Health check failed: Valkey server is still loading data.")
            return False

        if not is_primary and check_replica_sync and not self.is_replica_synced():
            logger.warning("Health check failed: Replica is not synced with primary.")
            return False

        return True

    def get_version(self) -> str:
        """Get the Valkey version from the server."""
        client = self._get_valkey_client()
        server_info = client.info_server(hostname=self.state.endpoint)

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

    def _save_database_blocking(self) -> None:
        """Run a synchronous save on the dataset and return when done, otherwise raise."""
        client = self._get_valkey_client()
        client.save(hostname=self.state.endpoint)

    def _disable_save_on_shutdown(self) -> None:
        """Set the configured shutdown option to 'no save' temporarily, will not persist a restart."""
        client = self._get_valkey_client()
        client.config_set(
            hostname=self.state.endpoint,
            config_settings={"shutdown-on-sigterm": "nosave"},
        )

    def save_dataset_before_shutdown(self) -> None:
        """Avoid a save-on-shutdown to be killed by Pebble, save and disable saving safely."""
        logger.info("Save dataset to disk")
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
        for temp_file in self.workload.working_dir.glob("temp-*.rdb"):
            logger.info("Removing temporary dump-file %s", temp_file)
            temp_file.unlink(missing_ok=True)

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
