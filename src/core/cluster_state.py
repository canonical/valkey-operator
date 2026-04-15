#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Objects representing the cluster state of Valkey."""

import logging

import ops
from data_platform_helpers.advanced_statuses.protocol import StatusesState, StatusesStateProtocol
from dpcharmlibs.interfaces import (
    OpsOtherPeerUnitRepositoryInterface,
    OpsPeerRepositoryInterface,
    OpsPeerUnitRepositoryInterface,
)

from core.models import PeerAppModel, PeerUnitModel, ValkeyCluster, ValkeyServer
from literals import (
    CLIENT_TLS_RELATION_NAME,
    EXTERNAL_CLIENTS_RELATION,
    PEER_RELATION,
    STATUS_PEERS_RELATION,
    Substrate,
)

logger = logging.getLogger(__name__)


class ClusterState(ops.Object, StatusesStateProtocol):
    """Global state object for the Valkey cluster."""

    def __init__(self, charm: ops.CharmBase, substrate: Substrate):
        super().__init__(parent=charm, key="charm_state")
        self.charm = charm
        self.peer_app_interface = OpsPeerRepositoryInterface(
            model=charm.model, relation_name=PEER_RELATION, data_model=PeerAppModel
        )
        self.peer_unit_interface = OpsPeerUnitRepositoryInterface(
            model=charm.model, relation_name=PEER_RELATION, data_model=PeerUnitModel
        )
        self.statuses_relation_name = STATUS_PEERS_RELATION
        self.statuses = StatusesState(self, self.statuses_relation_name)
        self.config = charm.config
        self.substrate = substrate

    @property
    def peer_relation(self) -> ops.model.Relation | None:
        """Get the Valkey peer relation."""
        return self.model.get_relation(PEER_RELATION)

    @property
    def peer_units_data_interfaces(
        self,
    ) -> dict[ops.model.Unit, OpsOtherPeerUnitRepositoryInterface[PeerUnitModel]]:
        """Get unit data interface of all peer units from the Valkey peer relation."""
        if not self.peer_relation or not self.peer_relation.units:
            return {}

        return {
            unit: OpsOtherPeerUnitRepositoryInterface(
                model=self.charm.model,
                relation_name=PEER_RELATION,
                unit=unit,
                data_model=PeerUnitModel,
            )
            for unit in self.peer_relation.units
        }

    @property
    def unit_server(self) -> ValkeyServer:
        """Get the server state of this unit."""
        return ValkeyServer(
            relation=self.peer_relation,
            data_interface=self.peer_unit_interface,
            component=self.model.unit,
        )

    @property
    def cluster(self) -> ValkeyCluster:
        """Get the cluster state of the entire Valkey application."""
        return ValkeyCluster(
            relation=self.peer_relation,
            data_interface=self.peer_app_interface,
            component=self.model.app,
        )

    @property
    def servers(self) -> set[ValkeyServer]:
        """Get all servers/units in the current peer relation, including this unit itself.

        Returns:
            Set of ValkeyServers with their unit data.
        """
        if not self.peer_relation:
            return set()

        servers = set()
        for unit, data_interface in self.peer_units_data_interfaces.items():
            servers.add(
                ValkeyServer(
                    relation=self.peer_relation,
                    data_interface=data_interface,
                    component=unit,
                )
            )
        servers.add(self.unit_server)

        return servers

    @property
    def client_tls_relation(self) -> ops.Relation | None:
        """Get the client certificates relation."""
        return self.model.get_relation(CLIENT_TLS_RELATION_NAME)

    @property
    def external_client_relations(self) -> set[ops.Relation]:
        """Get the client relations."""
        return set(self.model.relations[EXTERNAL_CLIENTS_RELATION])

    @property
    def bind_address(self) -> str:
        """The network binding address from the peer relation."""
        if not (binding := self.model.get_binding(self.peer_relation)):
            raise ValueError

        if not (address := binding.network.bind_address):
            raise ValueError

        return str(address)

    @property
    def ingress_address(self) -> str | None:
        """The network ingress address from the peer relation."""
        if not (binding := self.model.get_binding(self.peer_relation)):
            raise ValueError

        if not (address := binding.network.ingress_address):
            return None

        return str(address)

    @property
    def hostname(self) -> str:
        """The hostname of the unit."""
        return self.get_unit_hostname(self.model.unit.name)

    @property
    def endpoint(self) -> str:
        """The endpoint to be used by other units to connect to this unit.

        On VM-based substrates, this should be the bind address.
        On Kubernetes, this should be the fully qualified domain name of the unit.
        """
        return self.bind_address if self.substrate == Substrate.VM else self.hostname

    def get_secret_from_id(self, secret_id: str) -> dict[str, str]:
        """Resolve the given id of a Juju secret and return the content as a dict.

        Args:
            secret_id (str): The id of the secret.

        Returns:
            dict: The content of the secret.
        """
        try:
            secret_content = self.charm.model.get_secret(id=secret_id).get_content(refresh=True)
        except ops.SecretNotFoundError:
            raise ops.SecretNotFoundError(f"The secret '{secret_id}' does not exist.")
        except ops.ModelError:
            raise

        return secret_content

    def get_unit_hostname(self, unit_name: str | None = None) -> str:
        """Get the hostname.localdomain for a unit.

        Translate juju unit name to hostname.localdomain, necessary
        for correct name resolution under k8s.

        Args:
            unit_name: unit name
        Returns:
            A string representing the hostname.localdomain of the unit.
        """
        unit_name = unit_name or self.charm.unit.name
        return f"{unit_name.replace('/', '-')}.{self.charm.app.name}-endpoints"

    @property
    def number_units_started(self) -> int:
        """Return the number of units in the cluster that have their Valkey server started."""
        return len([unit for unit in self.servers if unit.model and unit.is_started])
