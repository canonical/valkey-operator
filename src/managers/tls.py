#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""Manager for handling TLS-related operations."""

import logging
from datetime import timedelta

from charmlibs.interfaces.tls_certificates import (
    Certificate,
    CertificateRequestAttributes,
    CertificateSigningRequest,
    PrivateKey,
    ProviderCertificate,
)
from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import TLSState
from statuses import CharmStatuses, TLSStatuses

logger = logging.getLogger(__name__)


class TLSManager(ManagerStatusProtocol):
    """Manage all TLS related events."""

    name: str = "tls"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload

    def set_tls_state(self, state: TLSState) -> None:
        """Set the TLS state.

        Args:
            state (TLSState): The TLS state.
        """
        logger.debug(f"Setting TLS state to {state}")
        self.state.unit_server.update({"tls_client_state": state.value})

    def set_cert_state(self, is_ready: bool) -> None:
        """Set the certificate state."""
        self.state.unit_server.update({"client_cert_ready": is_ready})

    def write_certificate(self, certificate: ProviderCertificate, private_key: PrivateKey) -> None:
        """Store the certificate on the unit.

        Args:
            certificate (ProviderCertificate): The certificate.
            private_key (PrivateKey): The private key.
        """
        self.workload.tls_dir.mkdir(exist_ok=True)
        self.workload.tls_paths.ca_certs_dir.mkdir(exist_ok=True)

        self.workload.write_file(private_key.raw, self.workload.tls_paths.client_key)
        self.workload.write_file(certificate.certificate.raw, self.workload.tls_paths.client_cert)
        self.workload.write_file(certificate.ca.raw, self.workload.tls_paths.client_ca)
        self.rehash_ca_certificates()

    def rehash_ca_certificates(self) -> None:
        """Generate hashed certificate names according to x509 format."""
        # using a CA directory for TLS requires hashed file links, see:
        # https://docs.openssl.org/1.1.1/man1/verify/#options
        cmd = ["c_rehash", self.workload.tls_paths.ca_certs_dir.as_posix()]
        self.workload.exec(cmd)

    def remove_certificate(self) -> None:
        """Remove the certificate from the unit."""
        self.workload.remove_file(self.workload.tls_paths.client_key)
        self.workload.remove_file(self.workload.tls_paths.client_cert)
        self.workload.remove_file(self.workload.tls_paths.client_ca)

    def build_common_name(self) -> str:
        """Build the Common Name for the TLS certificate."""
        # default common name, as per DA-166
        # https://docs.google.com/document/d/1OyxzBq5H4sFYZgmhWWVy5F4tQFmASQ7XKHVghxjW6go/edit?usp=sharing
        return f"{self.state.unit_server.unit_name.replace('/', '')}-{self.state.model.uuid}"

    def build_sans_ip(self) -> frozenset[str]:
        """Build the SANs IP for the TLS certificate."""
        sans_ip = set()

        if not self.state.peer_relation:
            return frozenset(sans_ip)

        sans_ip.add(self.state.bind_address)

        if ingress_ip := self.state.ingress_address:
            sans_ip.add(ingress_ip)

        return frozenset(sans_ip)

    def build_sans_dns(self) -> frozenset[str]:
        """Build the SANs DNS for the TLS certificate.

        Returns:
            frozenset[str]: The SANs DNS.
        """
        sans_dns = set()
        sans_dns.add(self.state.unit_server.unit_name.replace("/", ""))
        sans_dns.add(self.state.unit_server.hostname)

        return frozenset(sans_dns)

    def _generate_private_key(self) -> PrivateKey:
        """Generate a private key for use in peer TLS."""
        return PrivateKey.generate()

    def generate_ca_certificate(self) -> None:
        """Generate a CA certificate for use in peer TLS and store it to peer relation data."""
        private_key = self._generate_private_key()
        ca_attributes = CertificateRequestAttributes(
            common_name="Valkey Operator",
            is_ca=True,
            add_unique_id_to_subject_name=False,
        )

        ca_cert = Certificate.generate_self_signed_ca(
            attributes=ca_attributes,
            private_key=private_key,
            validity=timedelta(days=10950),
        )

        self.state.cluster.update({"internal_ca_certificate": ca_cert.raw})
        self.state.cluster.update({"internal_ca_private_key": private_key.raw})
        logger.info("Generated new self-signed CA certificate")

    def _generate_self_signed_certificate(self, private_key: PrivateKey) -> Certificate:
        """Generate a self-signed certificate for use in peer TLS."""
        cert_attributes = CertificateRequestAttributes(
            common_name=self.build_common_name(),
            sans_ip=self.build_sans_ip(),
            sans_dns=self.build_sans_dns(),
            is_ca=False,
            add_unique_id_to_subject_name=False,
        )
        certificate_signing_request = CertificateSigningRequest.generate(
            attributes=cert_attributes,
            private_key=private_key,
        )

        cert = Certificate.generate(
            csr=certificate_signing_request,
            ca=self.state.cluster.internal_ca_certificate,
            ca_private_key=self.state.cluster.internal_ca_private_key,
            validity=timedelta(days=10950),
            is_ca=False,
        )

        logger.info("Generated new self-signed certificate")
        return cert

    def create_and_store_self_signed_certificate(self) -> None:
        """Generate certificate files and store them for peer-TLS by default."""
        private_key = self._generate_private_key()
        certificate = self._generate_self_signed_certificate(private_key)
        ca_cert = self.state.cluster.internal_ca_certificate

        self.workload.tls_dir.mkdir(exist_ok=True)
        self.workload.tls_paths.ca_certs_dir.mkdir(exist_ok=True)

        self.workload.write_file(private_key.raw, self.workload.tls_paths.client_key)
        self.workload.write_file(certificate.raw, self.workload.tls_paths.client_cert)
        self.workload.write_file(ca_cert.raw, self.workload.tls_paths.client_ca)
        self.rehash_ca_certificates()

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the TLS statuses."""
        status_list: list[StatusObject] = []

        if self.state.unit_server.tls_client_state == TLSState.TO_TLS:
            status_list.append(TLSStatuses.ENABLING_CLIENT_TLS.value)

        if (
            self.state.unit_server.is_started
            and self.state.unit_server.tls_client_state == TLSState.TLS
            and not self.state.client_tls_relation
        ):
            status_list.append(TLSStatuses.DISABLING_CLIENT_TLS_FAILED.value)

        if self.state.unit_server.tls_client_state == TLSState.TO_NO_TLS:
            status_list.append(TLSStatuses.DISABLING_CLIENT_TLS.value)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
