#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Handlers for in-place upgrades."""

import abc
import dataclasses
import logging
from typing import TYPE_CHECKING

import charm_refresh

from common.exceptions import ValkeyCannotGetPrimaryIPError, ValkeyUpgradeError

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


@dataclasses.dataclass(eq=False)
class ValkeyRefresh(charm_refresh.CharmSpecificCommon, abc.ABC):
    """Shared code for upgrades."""

    charm: "ValkeyCharm"

    @classmethod
    def is_compatible(
        cls,
        *,
        old_charm_version: charm_refresh.CharmVersion,
        new_charm_version: charm_refresh.CharmVersion,
        old_workload_version: str,
        new_workload_version: str,
    ) -> bool:
        """Check charm version compatibility."""
        if not super().is_compatible(
            old_charm_version=old_charm_version,
            new_charm_version=new_charm_version,
            old_workload_version=old_workload_version,
            new_workload_version=new_workload_version,
        ):
            return False

        # Check workload version compatibility
        old_major, old_minor, old_patch = (
            int(component) for component in old_workload_version.split(".")
        )
        new_major, new_minor, new_patch = (
            int(component) for component in new_workload_version.split(".")
        )
        if old_major != new_major:
            return False

        if old_minor != new_minor:
            return new_minor > old_minor

        return new_patch >= old_patch

    def run_pre_refresh_checks_after_1_unit_refreshed(self) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Implement pre-refresh checks.

        These checks are run in three situations:
        - When the user runs the pre-refresh-check action on the leader before the refresh starts
        - On VM: after the user runs juju refresh and before any unit is refreshed
        - On K8s: after the user runs juju refresh and after the highest unit has refreshed
            and before this unit starts its workload
        """
        if not self.charm.state.unit_server.is_active:
            raise charm_refresh.PrecheckFailed("Unit is not started or being removed")

        if self.charm.state.unit_server.model.tls_certificate_expiring:
            raise charm_refresh.PrecheckFailed(
                "TLS certificates expiring soon, ensure renewal before upgrade"
            )

        if self.charm.state.unit_server.is_backup_in_progress:
            raise charm_refresh.PrecheckFailed("Backup in progress, wait for completion")

        if self.charm.state.cluster.is_restore_in_progress:
            raise charm_refresh.PrecheckFailed("Database restore in progress, cannot upgrade")

        if self.charm.state.unit_server.is_tls_transitioning:
            raise charm_refresh.PrecheckFailed(
                "TLS switchover or CA rotation in progress, cannot upgrade"
            )

    def run_pre_refresh_checks_before_any_units_refreshed(self) -> None:
        """Implement additional pre-refresh checks.

        These checks are only run in two situations:
        - When the user runs the pre-refresh-check action on the leader before the refresh starts
        - On VM: after the user runs juju refresh and before any unit is refreshed

        They can support health checks on the local unit.
        """
        self.run_pre_refresh_checks_after_1_unit_refreshed()

        try:
            primary_ip = self.charm.sentinel_manager.get_primary_ip()
        except ValkeyCannotGetPrimaryIPError:
            raise charm_refresh.PrecheckFailed("Primary not available, cannot upgrade")

        if not self.charm.cluster_manager.all_servers_healthy(primary_ip):
            raise charm_refresh.PrecheckFailed("Not all Valkey servers healthy, cannot upgrade")

        if not self.charm.sentinel_manager.all_sentinels_healthy():
            raise charm_refresh.PrecheckFailed("Not all Sentinels healthy, cannot upgrade")


@dataclasses.dataclass(eq=False)
class K8sValkeyRefresh(ValkeyRefresh, charm_refresh.CharmSpecificKubernetes):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Kubernetes-specific implementation for upgrades."""


@dataclasses.dataclass(eq=False)
class MachinesValkeyRefresh(ValkeyRefresh, charm_refresh.CharmSpecificMachines):  # pyright: ignore[reportIncompatibleMethodOverride]
    """VM-specific implementation for upgrades."""

    def refresh_snap(
        self,
        *,
        snap_name: str,
        snap_revision: str,
        refresh: charm_refresh.Machines,
    ) -> None:
        """Refresh the snap for the Valkey charm."""
        primary_ip = self.charm.sentinel_manager.get_primary_ip()
        active_sentinels = self.charm.sentinel_manager.get_active_sentinel_ips(primary_ip)
        if (
            primary_ip == self.charm.state.unit_server.get_endpoint(self.charm.state.substrate)
            and len(active_sentinels) > 1
        ):
            logger.debug("Triggering sentinel failover on primary IP %s", primary_ip)
            self.charm.sentinel_manager.failover()

        logger.info("Stopping workload before upgrade")
        self.charm.workload.stop()
        revision_before_refresh = self.charm.workload.snap_revision()  # pyright: ignore[reportAttributeAccessIssue]
        assert snap_revision != revision_before_refresh, (
            "current snap revision and target revision are equal"
        )

        logger.info("Updating snap installation")
        if not self.charm.workload.install(revision=snap_revision, retry_and_raise=False):  # pyright: ignore[reportAttributeAccessIssue]
            logger.exception("Snap refresh failed")
            if self.charm.workload.snap_revision() == revision_before_refresh:  # pyright: ignore[reportAttributeAccessIssue]
                self.charm.workload.start()
            else:
                refresh.update_snap_revision()

            # must raise an uncaught exception here to ensure the unit receives another Juju event
            raise ValkeyUpgradeError("Snap refresh failed")

        refresh.update_snap_revision()
        logger.info(f"Updated snap to revision {snap_revision}")

        self.charm.post_refresh_handling(refresh)
