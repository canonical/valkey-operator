#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""TLS related event handlers."""

import logging
from typing import TYPE_CHECKING

import ops
from charmlibs.interfaces.tls_certificates import (
    CertificateAvailableEvent,
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)

from common.exceptions import (
    ValkeyCertificatesNotReadyError,
    ValkeyTLSLoadError,
    ValkeyWorkloadCommandError,
)
from literals import CLIENT_PORT, CLIENT_TLS_RELATION_NAME, PEER_RELATION, TLSState

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class RefreshTLSCertificatesEvent(ops.EventBase):
    """Event for refreshing peer TLS certificates."""


class TLSEvents(ops.Object):
    """Handle all TLS related events."""

    refresh_tls_certificates_event = ops.EventSource(RefreshTLSCertificatesEvent)

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="tls_events")
        self.charm = charm

        self.client_certificate = TLSCertificatesRequiresV4(
            self.charm,
            CLIENT_TLS_RELATION_NAME,
            certificate_requests=[
                CertificateRequestAttributes(
                    common_name=self.charm.tls_manager.build_common_name(),
                    sans_ip=self.charm.tls_manager.build_sans_ip(),
                    sans_dns=self.charm.tls_manager.build_sans_dns(),
                ),
            ],
            private_key=None,
            refresh_events=[self.refresh_tls_certificates_event],
        )

        # --- EVENTS TO OBSERVE ---
        self.framework.observe(
            self.charm.on[CLIENT_TLS_RELATION_NAME].relation_created, self._on_tls_relation_created
        )
        self.framework.observe(
            self.charm.on[CLIENT_TLS_RELATION_NAME].relation_broken, self._on_tls_relation_broken
        )
        self.framework.observe(
            self.client_certificate.on.certificate_available, self._on_certificate_available
        )
        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_created, self._on_peer_relation_created
        )

    def _on_peer_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Set up self-signed certificates for peer TLS by default."""
        if self.charm.state.client_tls_relation:
            return

        if self.charm.unit.is_leader():
            self.charm.tls_manager.generate_ca_certificate()

        # in case a non-leader unit gets the event before the leader unit has processed it
        if not self.charm.state.cluster.internal_ca_certificate:
            logger.warning("Self-signed CA certificate not yet available")
            event.defer()
            return

        try:
            self.charm.tls_manager.create_and_store_self_signed_certificate()
        except ValkeyWorkloadCommandError as e:
            logger.error("Failed to create certificate for peer-TLS, startup will fail: %s", e)

    def _on_tls_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Handle the `relation-created` event."""
        self.charm.tls_manager.set_tls_state(TLSState.TO_TLS)

    def _on_certificate_available(self, event: CertificateAvailableEvent) -> None:
        """Handle the `certificate-available` event from TLS provider."""
        cert = event.certificate
        client_certificates, client_private_key = (
            self.client_certificate.get_assigned_certificates()
        )

        try:
            if client_certificates and client_certificates[0].certificate == cert:
                cert = client_certificates[0]
                private_key = client_private_key
                tls_state = self.charm.state.unit_server.tls_client_state
            else:
                logger.error("Received unknown certificate: %s", cert)
                return
        except IndexError:
            logger.error("Received certificate does not match provided certificates: %s", cert)
            return

        logger.info("Storing client certificate")
        try:
            self.charm.tls_manager.write_certificate(cert, private_key)
            self.charm.tls_manager.set_cert_state(is_ready=True)
        except ValkeyWorkloadCommandError as e:
            logger.error("Failed to store certificate: %s", e)
            event.defer()
            return

        if tls_state == TLSState.TO_TLS:
            try:
                self._enable_tls()
            except (ValkeyWorkloadCommandError, ValkeyTLSLoadError, ValueError):
                logger.error("Failed to enable client TLS")
                event.defer()
                return
            except ValkeyCertificatesNotReadyError:
                logger.warning("Not all units have stored the client certificate")
                event.defer()
                return

    def _on_tls_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Handle the `relation-broken` event."""
        if self.charm.app.planned_units() == 0:
            return

        if not self.charm.state.cluster.internal_ca_certificate:
            if self.charm.unit.is_leader():
                self.charm.tls_manager.generate_ca_certificate()
            else:
                logger.warning("Self-signed CA certificate not yet available")
                event.defer()
                return

        if (tls_state := self.charm.state.unit_server.tls_client_state) == TLSState.TLS:
            # only create self-signed certs once, in case disabling TLS gets deferred
            try:
                self.charm.tls_manager.create_and_store_self_signed_certificate()
            except ValkeyWorkloadCommandError as e:
                logger.error("Failed to create certificate for peer-TLS: %s", e)
                event.defer()
                return

        if tls_state in [TLSState.TLS, TLSState.TO_NO_TLS]:
            logger.info("Disabling client TLS")
            self.charm.tls_manager.set_tls_state(TLSState.TO_NO_TLS)
            try:
                self.charm.config_manager.set_config_properties(
                    self.charm.sentinel_manager.get_primary_ip()
                )
                tls_config = self.charm.config_manager.generate_tls_config()
                self.charm.cluster_manager.reload_tls_settings(tls_config)
            except (ValkeyWorkloadCommandError, ValkeyTLSLoadError, ValueError):
                logger.error("Failed to disable client TLS")
                event.defer()
                return

        self.charm.tls_manager.set_cert_state(is_ready=False)
        self.charm.tls_manager.set_tls_state(TLSState.NO_TLS)
        self.charm.unit.open_port("tcp", CLIENT_PORT)

    def _enable_tls(self) -> None:
        """Check preconditions and enable TLS if possible."""
        if not all(server.client_cert_ready for server in self.charm.state.servers):
            raise ValkeyCertificatesNotReadyError

        logger.info("Enabling TLS in Valkey")
        self.charm.config_manager.set_config_properties(
            self.charm.sentinel_manager.get_primary_ip()
        )
        tls_config = self.charm.config_manager.generate_tls_config()
        self.charm.cluster_manager.reload_tls_settings(tls_config)
        self.charm.tls_manager.set_tls_state(TLSState.TLS)
        self.charm.unit.close_port("tcp", CLIENT_PORT)
