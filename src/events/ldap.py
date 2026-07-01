#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""LDAP related event handlers."""

import logging
from typing import TYPE_CHECKING

import ops

# GLAuth is not compatible to certificate-transfer v1
from charms.certificate_transfer_interface.v0.certificate_transfer import (
    CertificateAvailableEvent,
    CertificateRemovedEvent,
    CertificateTransferRequires,
)
from charms.glauth_k8s.v0.ldap import (
    LdapReadyEvent,
    LdapRequirer,
    LdapUnavailableEvent,
)

from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyCannotGetPrimaryIPError,
    ValkeyWorkloadCommandError,
)
from literals import LDAP_CA_CERT_RELATION, LDAP_RELATION

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class LDAPEvents(ops.Object):
    """Handle all events related to LDAP."""

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="ldap_events")
        self.charm = charm

        self.ldap_requirer = LdapRequirer(self.charm, LDAP_RELATION)
        self.ldap_ca_transfer = CertificateTransferRequires(self.charm, LDAP_CA_CERT_RELATION)

        self.framework.observe(self.ldap_requirer.on.ldap_ready, self._on_ldap_ready)
        self.framework.observe(self.ldap_requirer.on.ldap_unavailable, self._on_ldap_unavailable)
        self.framework.observe(
            self.ldap_ca_transfer.on.certificate_available, self._on_ldap_ca_available
        )
        self.framework.observe(
            self.ldap_ca_transfer.on.certificate_removed, self._on_ldap_ca_removed
        )
        self.framework.observe(self.charm.on.config_changed, self._on_config_changed)
        self.framework.observe(self.charm.on.secret_changed, self._on_secret_changed)

    def _on_ldap_ready(self, event: LdapReadyEvent) -> None:
        """Handle the setup of the LDAP relation."""
        if not self.charm.state.is_ldap_valid:
            return

        try:
            self._update_ldap_config()
            self.charm.state.unit_server.update({"ldap_enabled": True})
        except (
            ValkeyACLLoadError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Failed to enable LDAP: %s", e)
            event.defer()
            return

    def _on_ldap_unavailable(self, event: LdapUnavailableEvent) -> None:
        """Handle the removal of the LDAP relation."""
        if not self.charm.state.unit_server.model.ldap_enabled:
            return

        try:
            self._update_ldap_config()
            self.charm.state.unit_server.update({"ldap_enabled": False})
        except (
            ValkeyACLLoadError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Failed to disable LDAP: %s", e)
            event.defer()
            return

    def _on_ldap_ca_available(self, event: CertificateAvailableEvent) -> None:
        """Handle the CA certificate available event for LDAP."""
        try:
            self.charm.workload.make_dir(self.charm.workload.tls_dir, exist_ok=True)
            self.charm.workload.write_file(
                content=event.ca, path=self.charm.workload.tls_paths.ldap_ca
            )
        except ValkeyWorkloadCommandError as e:
            logger.error("Error storing CA certificate for LDAP provider: %s", e)
            event.defer()
            return

        if not self.charm.state.is_ldap_valid:
            return

        try:
            self._update_ldap_config()
            self.charm.state.unit_server.update({"ldap_enabled": True})
        except (
            ValkeyACLLoadError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Failed to enable LDAP: %s", e)
            event.defer()
            return

    def _on_ldap_ca_removed(self, event: CertificateRemovedEvent) -> None:
        """Handle the CA certificate removed event for LDAP."""
        try:
            self.charm.workload.remove_file(self.charm.workload.tls_paths.ldap_ca)
        except ValkeyWorkloadCommandError as e:
            logger.error("Error removing CA certificate for LDAP: %s", e)
            event.defer()
            return

        if not self.charm.state.unit_server.model.ldap_enabled:
            return

        try:
            self._update_ldap_config()
            self.charm.state.unit_server.update({"ldap_enabled": False})
        except (
            ValkeyACLLoadError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Failed to disable LDAP: %s", e)
            event.defer()
            return

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Handle LDAP related config changes."""
        if not self.charm.state.is_ldap_valid:
            return

        try:
            self._update_ldap_config()
            self.charm.state.unit_server.update({"ldap_enabled": True})
        except (
            ValkeyACLLoadError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Failed to update LDAP settings: %s", e)
            event.defer()
            return

    def _on_secret_changed(self, event: ops.SecretChangedEvent) -> None:
        """Handle LDAP related secret updates."""
        if not self.charm.state.is_ldap_valid:
            return

        if event.secret.id != self.charm.state.ldap.bind_password_secret:
            return

        try:
            self._update_ldap_config()
        except (
            ValkeyACLLoadError,
            ValkeyCannotGetPrimaryIPError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error("Failed to update LDAP settings: %s", e)
            event.defer()
            return

    def _update_ldap_config(self) -> None:
        """Update the current LDAP configuration."""
        if not self.charm.state.unit_server.is_started:
            return

        logger.info("Update LDAP configuration")
        primary_ip = self.charm.sentinel_manager.get_primary_ip()
        self.charm.config_manager.set_config_properties(primary_endpoint=primary_ip)
        ldap_config = self.charm.config_manager.generate_ldap_config()
        self.charm.cluster_manager.reload_ldap_settings(ldap_config)

        logger.info("Update ACL configuration")
        # only update Valkey ACLs, we do not support LDAP for Sentinel
        self.charm.auth_manager.set_acl_file()
        self.charm.cluster_manager.reload_acl_file()
