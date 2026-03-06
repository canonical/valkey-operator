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
from literals import TLSCARotationState, TLSState
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

    def set_ca_rotation_state(self, state: TLSCARotationState) -> None:
        """Set the CA rotation state.

        Args:
            state (TLSCARotationState): The CA rotation state.
        """
        logger.debug(f"Setting TLS CA rotation state to {state}")
        self.state.unit_server.update({"tls_ca_rotation": state.value})

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

    def start_ca_rotation_if_required(
        self, certificate: ProviderCertificate | None = None
    ) -> bool:
        """Check a certificate if the CA is new and if so, start the CA rotation on this unit.

        Args:
            certificate (ProviderCertificate): The certificate to check. If not given,
                the internal CA cert from the peer relation will be used.

        Returns:
            True if CA rotation was started, False if not
        """
        if self.state.unit_server.tls_ca_rotation_state != TLSCARotationState.NO_ROTATION:
            # safeguard in case another new certificate arrives during a CA rotation
            logger.debug("CA rotation already in progress")
            return True

        if not self.workload.tls_paths.client_ca.exists():
            logger.debug("No CA rotation, no previous CA cert stored")
            return False

        if len(self.state.servers) == 1:
            logger.debug("No CA rotation orchestration in case of a single unit")
            return False

        if certificate:
            ca_cert = certificate.ca
        else:
            ca_cert = self.state.cluster.internal_ca_certificate
        current_ca_cert = self.workload.read_file(self.workload.tls_paths.client_ca)

        if ca_cert.raw == current_ca_cert:
            logger.debug("No CA rotation, CA cert is up-to-date")
            return False

        logger.info("New CA certificate detected")
        self.workload.write_file(
            current_ca_cert, self.workload.tls_paths.client_ca.with_name("old_client_ca.pem")
        )

        self.set_ca_rotation_state(TLSCARotationState.NEW_CA_DETECTED)
        return True

    def rehash_ca_certificates(self) -> None:
        """Generate hashed certificate names according to x509 format."""
        # using a CA directory for TLS requires hashed file links, see:
        # https://docs.openssl.org/1.1.1/man1/verify/#options
        cmd = ["c_rehash", self.workload.tls_paths.ca_certs_dir.as_posix()]
        self.workload.exec(cmd)

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

        if not self.state.peer_relation:
            return frozenset(sans_dns)

        sans_dns.add(self.state.unit_server.model.hostname)

        return frozenset(sans_dns)

    def _generate_private_key(self) -> PrivateKey:
        """Generate a private key for use in peer TLS."""
        return PrivateKey.generate()

    def generate_ca_certificate(self) -> None:
        """Generate a CA certificate for use in peer TLS and store it to peer relation data."""
        private_key = self._generate_private_key()
        ca_attributes = CertificateRequestAttributes(
            common_name="Valkey-Operator",
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

    def check_certificate_validity(self) -> None:
        """Check if the certificates installed on the unit will soon expire."""
        for cert_file in [
            self.workload.tls_paths.client_cert.as_posix(),
            self.workload.tls_paths.client_ca.as_posix(),
        ]:
            # will raise if cert expires in less than 24h (=86400s)
            self.workload.exec(
                [
                    "openssl",
                    "x509",
                    "-checkend",
                    "86400",
                    "-noout",
                    "-in",
                    cert_file,
                ]
            )

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the TLS statuses."""
        status_list: list[StatusObject] = []

        # Peer relation not established yet, or model not built yet for unit or app
        if not self.state.cluster.model or not self.state.unit_server.model:
            return status_list or [CharmStatuses.ACTIVE_IDLE.value]

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

        if self.state.unit_server.model.tls_certificate_expiring:
            status_list.append(TLSStatuses.CERTIFICATE_EXPIRING.value)

        match self.state.unit_server.tls_ca_rotation_state:
            case TLSCARotationState.NEW_CA_DETECTED:
                status_list.append(TLSStatuses.CA_ROTATION_DETECTED.value)
            case TLSCARotationState.NEW_CA_ADDED:
                status_list.append(TLSStatuses.CA_ROTATION_CA_ADDED.value)
            case TLSCARotationState.CA_UPDATED:
                status_list.append(TLSStatuses.CA_ROTATION_UPDATED.value)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
