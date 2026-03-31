#!/usr/bin/env python3
# Copyright 2026 rene.radoi@canonical.com
# See LICENSE file for licensing details.

"""Charm the application."""

import asyncio
import logging
import socket

import ops
from charmlibs.interfaces.tls_certificates import (
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)
from charms.data_platform_libs.v0.data_interfaces import DatabaseCreatedEvent, DatabaseRequires
from charms.data_platform_libs.v1.data_interfaces import (
    DataContractV1,
    RequirerCommonModel,
    ResourceCreatedEvent,
    ResourceEndpointsChangedEvent,
    ResourceProviderModel,
    ResourceRequirerEventHandler,
    ValkeyResponseModel,
    build_model,
)
from client import ValkeyClient

logger = logging.getLogger(__name__)

SERVICE_NAME = "some-service"  # Name of Pebble service that runs in the workload container.


class RequirerCharm(ops.CharmBase):
    """Charm that acts as client for Valkey."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        if self.config.get("data-interfaces-version") == 0:
            self.data_interfaces_version = 0
        else:
            self.data_interfaces_version = 1

        self.certificates = TLSCertificatesRequiresV4(
            self,
            "certificates",
            certificate_requests=[
                CertificateRequestAttributes(
                    common_name="requirer-charm",
                    sans_ip=frozenset({socket.gethostbyname(socket.gethostname())}),
                    sans_dns=frozenset({self.unit.name, socket.gethostname()}),
                )
            ],
        )

        if self.data_interfaces_version == 1:
            self.valkey_interface = ResourceRequirerEventHandler(
                charm=self,
                relation_name="valkey-client",
                requests=[
                    RequirerCommonModel(resource="requirer-charm:*"),
                    RequirerCommonModel(resource="*"),
                ],
                response_model=ValkeyResponseModel,
            )
            self.framework.observe(
                self.valkey_interface.on.resource_created, self._on_resource_created
            )
        else:
            self.valkey_interface = DatabaseRequires(
                charm=self,
                relation_name="valkey-client",
                database_name="requirer-charm:*",
            )
            self.framework.observe(
                self.valkey_interface.on.database_created, self._on_database_created
            )

        # Event observers
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.set_action, self._on_set_action)
        framework.observe(self.on.get_action, self._on_get_action)
        framework.observe(self.on.get_credentials_action, self._on_get_credentials_action)
        framework.observe(self.valkey_interface.on.endpoints_changed, self._on_endpoints_changed)

    @property
    def valkey_relation(self) -> ops.Relation | None:
        if not (relations := self.valkey_interface.relations):
            return None

        return relations[0]

    @property
    def remote_responses(self) -> list[ResourceProviderModel] | None:
        """Return the remote response model."""
        if not self.valkey_relation:
            return None

        return build_model(
            self.valkey_interface.interface.repository(
                self.valkey_relation.id, self.valkey_relation.app
            ),
            DataContractV1[ResourceProviderModel],
        ).requests

    @property
    def credentials(self) -> dict[str | None, str | None]:
        """Retrieve the client credentials provided by Valkey."""
        if self.data_interfaces_version == 0:
            if not self.valkey_relation:
                return {"": None}

            return {
                self.valkey_interface.fetch_relation_field(
                    self.valkey_relation.id, "username"
                ): self.valkey_interface.fetch_relation_field(self.valkey_relation.id, "password")
            }

        remote_responses = self.remote_responses
        if not remote_responses:
            return {"": None}

        credentials = {}
        for response in remote_responses:
            credentials.update({response.username: response.password})

        return credentials

    @property
    def primary_endpoint(self) -> str | None:
        """Retrieve the write-endpoints provided by Valkey."""
        if self.data_interfaces_version == 0:
            if not self.valkey_relation:
                return None

            return self.valkey_interface.fetch_relation_field(self.valkey_relation.id, "endpoints")

        remote_responses = self.remote_responses
        if not remote_responses:
            return None

        return remote_responses[0].endpoints

    @property
    def tls_enabled(self) -> bool:
        """Retrieve the tls flag provided by Valkey."""
        if self.data_interfaces_version == 0:
            if not self.valkey_relation:
                return False

            return (
                self.valkey_interface.fetch_relation_field(self.valkey_relation.id, "tls")
                == "true"
            )

        remote_responses = self.remote_responses
        if not remote_responses:
            return False

        return remote_responses[0].tls

    @property
    def tls_ca_cert(self) -> str | None:
        """Retrieve the tls CA cert provided by Valkey."""
        if self.data_interfaces_version == 0:
            if not self.valkey_relation:
                return None

            return self.valkey_interface.fetch_relation_field(self.valkey_relation.id, "tls-ca")

        remote_responses = self.remote_responses
        if not remote_responses:
            return None

        return remote_responses[0].tls_ca

    @property
    def certificate(self) -> str | None:
        certificates, _ = self.certificates.get_assigned_certificates()
        if not certificates:
            return None

        return certificates[0].certificate.raw

    @property
    def private_key(self) -> str | None:
        _, private_key = self.certificates.get_assigned_certificates()
        if not private_key:
            return None

        return private_key.raw

    def _on_start(self, event: ops.StartEvent) -> None:
        """Handle start event."""
        self.unit.status = ops.ActiveStatus()

    def _on_set_action(self, event: ops.ActionEvent) -> None:
        """Handle set action."""
        if not self.valkey_relation:
            event.fail("The action can be run only after relation is created.")
            event.set_results({"ok": False})
            return

        key = str(event.params.get("key", ""))
        value = str(event.params.get("value", ""))
        user = str(event.params.get("user", ""))
        if not key or not value or not user:
            event.fail("Parameters key, value and user are required.")
            event.set_results({"ok": False})
            return

        client = ValkeyClient(
            username=user,
            password=self.credentials.get(user),
            host=self.primary_endpoint.split(":")[0],
            port=int(self.primary_endpoint.split(":")[1]),
            tls_cert=self.certificate.encode() if self.tls_enabled else None,
            tls_key=self.private_key.encode() if self.tls_enabled else None,
            tls_ca_cert=self.tls_ca_cert.encode() if self.tls_enabled else None,
        )
        try:
            asyncio.run(client.set_key(key, value))
            event.set_results({"ok": True})
        except Exception as e:
            event.fail(f"Failed to write data: {e}")
            logger.error("Failed to write data: %s", e)

    def _on_get_action(self, event: ops.ActionEvent) -> None:
        """Handle get action."""
        if not self.valkey_relation:
            event.fail("The action can be run only after relation is created.")
            event.set_results({"ok": False})
            return

        key = str(event.params.get("key", ""))
        user = str(event.params.get("user", ""))
        if not key or not user:
            event.fail("Parameters key and user are required.")
            event.set_results({"ok": False})
            return

        client = ValkeyClient(
            username=user,
            password=self.credentials.get(user),
            host=self.primary_endpoint.split(":")[0],
            port=int(self.primary_endpoint.split(":")[1]),
            tls_cert=self.certificate.encode() if self.tls_enabled else None,
            tls_key=self.private_key.encode() if self.tls_enabled else None,
            tls_ca_cert=self.tls_ca_cert.encode() if self.tls_enabled else None,
        )
        try:
            value = asyncio.run(client.get_key(key))
            event.set_results(
                {
                    "ok": True,
                    "result": value,
                }
            )
        except Exception as e:
            event.fail(f"Failed to write data: {e}")
            logger.error("Failed to read data: %s", e)

    def _on_get_credentials_action(self, event: ops.ActionEvent) -> None:
        """Return the credentials an action response."""
        if not self.valkey_relation:
            event.fail("The action can be run only after relation is created.")
            event.set_results({"ok": False})
            return

        credentials = self.credentials
        usernames = ",".join(list(credentials.keys()))
        event.set_results(
            {
                "ok": True,
                "usernames": usernames,
            }
        )

    def _on_resource_created(self, event: ResourceCreatedEvent[ResourceProviderModel]) -> None:
        """Handle resource created event."""
        logger.info("Resource created")

    def _on_endpoints_changed(
        self, event: ResourceEndpointsChangedEvent[ResourceProviderModel]
    ) -> None:
        """Handle endpoints changed event."""
        logger.info("Valkey endpoints have been changed")

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        """Handle the event triggered by data-interfaces v0."""
        logger.info("Database created")


if __name__ == "__main__":  # pragma: nocover
    ops.main(RequirerCharm)
