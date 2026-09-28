#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Handlers for in-place upgrades."""

import abc
import dataclasses
import logging
from typing import TYPE_CHECKING

import charm_refresh

from common.exceptions import ValkeyUpgradeError

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

    @staticmethod
    def run_pre_refresh_checks_after_1_unit_refreshed() -> None:
        """Implement pre-refresh checks after 1 unit refreshed."""
        pass


@dataclasses.dataclass(eq=False)
class K8sValkeyRefresh(ValkeyRefresh, charm_refresh.CharmSpecificKubernetes):
    """Kubernetes-specific implementation for upgrades."""

    @staticmethod
    def run_pre_refresh_checks_after_1_unit_refreshed() -> None:
        """Implement pre-refresh checks after 1 unit refreshed."""
        pass


@dataclasses.dataclass(eq=False)
class MachinesValkeyRefresh(ValkeyRefresh, charm_refresh.CharmSpecificMachines):
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
            # must raise an uncaught exception her to ensure the unit receives another Juju event
            raise ValkeyUpgradeError("Snap refresh failed")

        refresh.update_snap_revision()
        logger.info(f"Updated snap to revision {snap_revision}")

        logger.info("Restarting workload")
        # query primary again, snap install can take long and there might have been a failover
        primary_ip = self.charm.sentinel_manager.get_primary_ip()
        self.charm.auth_manager.configure_auth()
        self.charm.config_manager.configure_services(primary_ip)
        self.charm.metrics_manager.reconcile()
        self.charm.workload.start()

        logger.info("Confirming health after upgrade")
        if (
            self.charm.cluster_manager.is_healthy(
                # only check replica sync if there is another unit that can be primary
                check_replica_sync=len(active_sentinels) > 1
            )
            and self.charm.sentinel_manager.is_healthy()
        ):
            refresh.next_unit_allowed_to_refresh = True
