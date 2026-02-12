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
from literals import (
    CLIENT_TLS_RELATION_NAME,
    PEER_TLS_RELATION_NAME,
    TLSState,
    TLSType,
)

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
                    sans_ip=self.charm.tls_manager.build_sans_ip(TLSType.CLIENT),
                    sans_dns=self.charm.tls_manager.build_sans_dns(),
                ),
            ],
            private_key=None,
            refresh_events=[self.refresh_tls_certificates_event],
        )

        self.peer_certificate = TLSCertificatesRequiresV4(
            self.charm,
            PEER_TLS_RELATION_NAME,
            certificate_requests=[
                CertificateRequestAttributes(
                    common_name=self.charm.tls_manager.build_common_name(),
                    sans_ip=self.charm.tls_manager.build_sans_ip(TLSType.PEER),
                    sans_dns=self.charm.tls_manager.build_sans_dns(),
                ),
            ],
            private_key=None,
            refresh_events=[self.refresh_tls_certificates_event],
        )

        # --- EVENTS TO OBSERVE ---
        for relation in [CLIENT_TLS_RELATION_NAME, PEER_TLS_RELATION_NAME]:
            self.framework.observe(
                self.charm.on[relation].relation_created, self._on_relation_created
            )
            self.framework.observe(
                self.charm.on[relation].relation_broken, self._on_relation_broken
            )
        for relation in [self.peer_certificate, self.client_certificate]:
            self.framework.observe(
                relation.on.certificate_available, self._on_certificate_available
            )

    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Handle the `relation-created` event."""
        if event.relation.name == CLIENT_TLS_RELATION_NAME:
            self.charm.tls_manager.set_tls_state(tls_type=TLSType.CLIENT, state=TLSState.TO_TLS)
            return

        self.charm.tls_manager.set_tls_state(tls_type=TLSType.PEER, state=TLSState.TO_TLS)

    def _on_certificate_available(self, event: CertificateAvailableEvent) -> None:
        """Handle the `certificate-available` event from TLS provider."""
        cert = event.certificate
        client_certificates, client_private_key = (
            self.client_certificate.get_assigned_certificates()
        )
        peer_certificates, peer_private_key = self.peer_certificate.get_assigned_certificates()

        try:
            if client_certificates and client_certificates[0].certificate == cert:
                cert_type = TLSType.CLIENT
                cert = client_certificates[0]
                private_key = client_private_key
                tls_state = self.charm.state.unit_server.tls_client_state
            elif peer_certificates and peer_certificates[0].certificate == cert:
                cert_type = TLSType.PEER
                cert = peer_certificates[0]
                private_key = peer_private_key
                tls_state = self.charm.state.unit_server.tls_peer_state
            else:
                logger.error("Received unknown certificate: %s", cert)
                return
        except IndexError:
            logger.error("Received certificate does not match provided certificates: %s", cert)
            return

        if (
            cert_type == TLSType.PEER
            and self.charm.state.unit_server.tls_client_state != TLSState.TLS
        ):
            logger.warning("Cannot enable peer TLS if client TLS is not enabled")
            event.defer()
            return

        logger.info("Storing %s certificate", cert_type.value)
        try:
            self.charm.tls_manager.write_certificate(cert_type, cert, private_key)
            self.charm.tls_manager.set_cert_state(cert_type, is_ready=True)
        except ValkeyWorkloadCommandError as e:
            logger.error("Failed to store certificate: %s", e)
            event.defer()
            return

        if tls_state == TLSState.TO_TLS:
            try:
                self._enable_tls(cert_type)
            except (ValkeyWorkloadCommandError, ValkeyTLSLoadError, ValueError):
                logger.error("Failed to enable %s TLS", cert_type)
                event.defer()
                return
            except ValkeyCertificatesNotReadyError:
                logger.warning("Not all units have stored the %s certificate", cert_type)
                event.defer()
                return

    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Handle the `relation-broken` event."""
        if event.relation.name == CLIENT_TLS_RELATION_NAME:
            tls_type = TLSType.CLIENT
            tls_state = self.charm.state.unit_server.tls_client_state
        else:
            tls_type = TLSType.PEER
            tls_state = self.charm.state.unit_server.tls_peer_state

        if tls_state in [TLSState.TLS, TLSState.TO_NO_TLS]:
            logger.info("Disabling %s TLS", tls_type)
            self.charm.tls_manager.set_tls_state(tls_type, TLSState.TO_NO_TLS)
            try:
                self.charm.config_manager.set_config_properties()
                if tls_type == TLSType.CLIENT:
                    self.charm.cluster_manager.disable_tls_settings()
                else:
                    self.charm.cluster_manager.reload_tls_settings()
            except (ValkeyWorkloadCommandError, ValkeyTLSLoadError, ValueError):
                logger.error("Failed to disable %s TLS", tls_type)
                event.defer()
                return

        self.charm.tls_manager.remove_certificate(tls_type)
        self.charm.tls_manager.set_cert_state(tls_type, is_ready=False)
        self.charm.tls_manager.set_tls_state(tls_type, TLSState.NO_TLS)

    def _enable_tls(self, tls_type: TLSType) -> None:
        """Check preconditions and enable TLS if possible."""
        if (
            tls_type == TLSType.CLIENT
            and not all(server.client_cert_ready for server in self.charm.state.servers)
            or tls_type == TLSType.PEER
            and not all(server.peer_cert_ready for server in self.charm.state.servers)
        ):
            raise ValkeyCertificatesNotReadyError

        logger.info("Enabling %s TLS in Valkey", tls_type)
        self.charm.config_manager.set_config_properties()
        tls_config = self.charm.config_manager.generate_tls_config()
        self.charm.cluster_manager.enable_tls_settings(tls_config)
        self.charm.tls_manager.set_tls_state(tls_type, TLSState.TLS)
