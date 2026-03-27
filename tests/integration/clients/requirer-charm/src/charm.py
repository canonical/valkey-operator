#!/usr/bin/env python3
# Copyright 2026 rene.radoi@canonical.com
# See LICENSE file for licensing details.

"""Charm the application."""

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
        framework.observe(self.on.put_action, self._on_put_action)
        framework.observe(self.on.get_action, self._on_get_action)
        self.framework.observe(
            self.valkey_interface.on.endpoints_changed, self._on_endpoints_changed
        )

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
    def credentials(self) -> dict[str, str] | None:
        """Retrieve the client credentials provided by Valkey."""
        remote_responses = self.remote_responses
        if not remote_responses:
            return None

        credentials = {}
        for response in remote_responses:
            credentials.update({response.username: response.password})

        return credentials

    @property
    def primary_endpoints(self) -> str | None:
        """Retrieve the write-endpoints provided by Valkey."""
        remote_responses = self.remote_responses
        if not remote_responses:
            return None

        return remote_responses[0].endpoints

    @property
    def replica_endpoints(self) -> str | None:
        """Retrieve the read-only-endpoints provided by Valkey."""
        remote_responses = self.remote_responses
        if not remote_responses:
            return None

        return remote_responses[0].read_only_endpoints

    def _on_start(self, event: ops.StartEvent) -> None:
        """Handle start event."""
        self.unit.status = ops.ActiveStatus()

    def _on_put_action(self, event: ops.ActionEvent) -> None:
        """Handle put action."""
        if not self.valkey_relation:
            event.fail("The action can be run only after relation is created.")
            event.set_results({"ok": False})
            return

        if not (key := str(event.params.get("key", ""))) or not (value := str(event.params.get("value", ""))):
            event.fail("Both key and value parameters are required.")
            event.set_results({"ok": False})
            return

        # todo: add logic

    def _on_get_action(self, event: ops.ActionEvent) -> None:
        """Handle get action."""
        if not self.valkey_relation:
            event.fail("The action can be run only after relation is created.")
            event.set_results({"ok": False})
            return

        if not (key := str(event.params.get("key", ""))):
            event.fail("Key parameter is required.")
            event.set_results({"ok": False})
            return

        # todo: add logic

    def _on_resource_created(self, event: ResourceCreatedEvent[ResourceProviderModel]) -> None:
        """Handle resource created event."""
        logger.info("Resource created")
        logger.info("Valkey endpoints: %s", event.response.endpoints)

    def _on_endpoints_changed(self, event: ResourceEndpointsChangedEvent[ResourceProviderModel]) -> None:
        """Handle endpoints changed event."""
        logger.info("Valkey endpoints have been changed to: %s", event.response.endpoints)

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        """Handle the event triggered by data-interfaces v0."""
        logger.info("Database created")
        logger.info("Valkey endpoints: %s", event.endpoints)


if __name__ == "__main__":  # pragma: nocover
    ops.main(RequirerCharm)
