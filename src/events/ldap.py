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

from common.exceptions import ValkeyWorkloadCommandError
from literals import LDAP_CA_CERT_RELATION

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class LDAPEvents(ops.Object):
    """Handle all events related to LDAP."""

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="ldap_events")
        self.charm = charm

        self.ldap_ca_transfer = CertificateTransferRequires(self.charm, LDAP_CA_CERT_RELATION)
        self.framework.observe(
            self.ldap_ca_transfer.on.certificate_available, self._on_ca_available
        )
        self.framework.observe(self.ldap_ca_transfer.on.certificate_removed, self._on_ca_removed)

    def _on_ca_available(self, event: CertificateAvailableEvent) -> None:
        """Handle the CA certificate available event for LDAP."""
        try:
            self.charm.workload.make_dir(self.charm.workload.tls_dir, exist_ok=True)
            self.charm.workload.write_file(
                content="\n".join(event.chain), path=self.charm.workload.tls_paths.ldap_ca
            )
        except ValkeyWorkloadCommandError as e:
            logger.error("Error storing CA certificate for LDAP provider: %s", e)
            event.defer()
            return

    def _on_ca_removed(self, event: CertificateRemovedEvent) -> None:
        """Handle the CA certificate removed event for LDAP."""
        try:
            self.charm.workload.remove_file(self.charm.workload.tls_paths.ldap_ca)
        except ValkeyWorkloadCommandError as e:
            logger.error("Error removing CA certificate for LDAP: %s", e)
            event.defer()
            return
