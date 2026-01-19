#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Objects representing the cluster state of Valkey."""

import logging

import ops
from charms.data_platform_libs.v1.data_interfaces import (
    OpsOtherPeerUnitRepositoryInterface,
    OpsPeerRepositoryInterface,
    OpsPeerUnitRepositoryInterface,
)
from data_platform_helpers.advanced_statuses.protocol import StatusesState, StatusesStateProtocol

from core.models import PeerAppModel, PeerUnitModel, ValkeyCluster, ValkeyServer
from literals import PEER_RELATION, STATUS_PEERS_RELATION

logger = logging.getLogger(__name__)


class ClusterState(ops.Object, StatusesStateProtocol):
    """Global state object for the Valkey cluster."""

    def __init__(self, charm: ops.CharmBase):
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

    def get_secret_from_id(self, secret_id: str) -> dict[str, str]:
        """Resolve the given id of a Juju secret and return the content as a dict.

        Args:
            model (Model): Model object.
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
