#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""Manager for handling TLS-related operations."""

import logging

from charmlibs.interfaces.tls_certificates import (
    PrivateKey,
    ProviderCertificate,
)
from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import TLSState, TLSType
from statuses import CharmStatuses, TLSStatuses

logger = logging.getLogger(__name__)


class TLSManager(ManagerStatusProtocol):
    """Manage all TLS related events."""

    name: str = "tls"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload

    def set_tls_state(self, tls_type: TLSType, state: TLSState) -> None:
        """Set the TLS state.

        Args:
            tls_type (TLSType): The TLS type: peer or client.
            state (TLSState): The TLS state.
        """
        logger.debug(f"Setting {tls_type} TLS state to {state}")
        self.state.unit_server.update(
            {
                f"tls_{tls_type.value}_state": state.value,
            }
        )

    def set_cert_state(self, cert_type: TLSType, is_ready: bool) -> None:
        """Set the certificate state.

        Args:
            cert_type (TLSType): The certificate type: peer or client.
            is_ready (bool): The certificate state.
        """
        self.state.unit_server.update({f"{cert_type.value}_cert_ready": is_ready})

    def write_certificate(
        self, cert_type: TLSType, certificate: ProviderCertificate, private_key: PrivateKey
    ) -> None:
        """Store the certificate on the unit.

        Args:
            certificate (ProviderCertificate): The certificate.
            private_key (PrivateKey): The private key.
            cert_type (TLSType): The certificate type: client or peer.
        """
        self.workload.tls_dir.mkdir(exist_ok=True)
        self.workload.tls_paths.ca_certs_dir.mkdir(exist_ok=True)

        if cert_type == TLSType.CLIENT:
            certificate_path = self.workload.tls_paths.client_cert
            private_key_path = self.workload.tls_paths.client_key
            ca_cert_path = self.workload.tls_paths.client_ca
        else:
            certificate_path = self.workload.tls_paths.peer_cert
            private_key_path = self.workload.tls_paths.peer_key
            ca_cert_path = self.workload.tls_paths.peer_ca

        self.workload.write_file(private_key.raw, private_key_path)
        self.workload.write_file(certificate.certificate.raw, certificate_path)
        self.workload.write_file(certificate.ca.raw, ca_cert_path)

    def remove_certificate(self, cert_type: TLSType) -> None:
        """Remove the certificate from the unit.

        Args:
            cert_type (TLSType): The certificate type: client or peer.
        """
        if cert_type == TLSType.CLIENT:
            certificate_path = self.workload.tls_paths.client_cert
            private_key_path = self.workload.tls_paths.client_key
            ca_cert_path = self.workload.tls_paths.client_ca
        else:
            certificate_path = self.workload.tls_paths.peer_cert
            private_key_path = self.workload.tls_paths.peer_key
            ca_cert_path = self.workload.tls_paths.peer_ca

        self.workload.remove_file(private_key_path)
        self.workload.remove_file(certificate_path)
        self.workload.remove_file(ca_cert_path)

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the TLS statuses."""
        status_list: list[StatusObject] = []

        if (
            self.state.peer_tls_relation
            and self.state.unit_server.tls_client_state != TLSState.TLS
        ):
            status_list.append(TLSStatuses.MISSING_CLIENT_TLS.value)

        if self.state.unit_server.tls_client_state == TLSState.TO_TLS:
            status_list.append(TLSStatuses.ENABLING_CLIENT_TLS.value)

        if self.state.unit_server.tls_peer_state == TLSState.TO_TLS:
            status_list.append(TLSStatuses.ENABLING_PEER_TLS.value)

        if self.state.unit_server.tls_client_state == TLSState.TO_NO_TLS:
            status_list.append(TLSStatuses.DISABLING_CLIENT_TLS.value)

        if self.state.unit_server.tls_peer_state == TLSState.TO_NO_TLS:
            status_list.append(TLSStatuses.DISABLING_PEER_TLS.value)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
