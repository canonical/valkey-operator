#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for handling external clients."""

import logging

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from statuses import CharmStatuses, ExternalClientsStatuses

logger = logging.getLogger(__name__)


class ExternalClientsManager(ManagerStatusProtocol):
    """Manage business logic for external clients."""

    name: str = "external_clients"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload

    @staticmethod
    def get_username(relation_id: int, request_id: str | None) -> str:
        """Get the username for a specific request on a relation.

        Args:
            relation_id (str): The id of the relation with the external client.
            request_id (str): The id of the request from the client relation.
        """
        return f"relation-{relation_id}-{request_id}" if request_id else f"relation-{relation_id}"

    def add_managed_user_if_required(self, username: str, password: str, resource: str) -> None:
        """Add an external client's user to the state."""
        if not (external_client_users := self.state.cluster.external_users_credentials):
            external_client_users = {}

        if external_client_users.get(username):
            logger.debug("Client user already exists: %s", username)
            return

        logger.info("Adding managed user %s", username)
        external_client_users.update(
            {
                username: {
                    "password": password,
                    "resource": resource,
                }
            }
        )
        self.state.cluster.update({"external_client_users": external_client_users})

    def remove_managed_users(self, relation_id: int):
        """Remove all managed users for an external client relation from the state."""
        if not (external_client_users := self.state.cluster.external_users_credentials):
            return

        for username in list(external_client_users):
            if username.startswith(f"relation-{relation_id}"):
                logger.info("Removing managed user %s", username)
                del external_client_users[username]

        self.state.cluster.update({"external_client_users": external_client_users})

    def get_password(self, username: str) -> str | None:
        """Query the password of an external client user from the state."""
        if not (external_client_users := self.state.cluster.external_users_credentials):
            return None

        if user := external_client_users.get(username):
            return user.get("password")

        return None

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the external client statuses."""
        status_list: list[StatusObject] = []

        # Peer relation not established yet, model not built yet or no users added
        if (
            not self.state.cluster.model
            or not self.state.unit_server.model
            or not self.state.external_client_relations
            or scope != "app"
        ):
            return status_list or [CharmStatuses.ACTIVE_IDLE.value]

        if not self.state.cluster.external_users_credentials:
            status_list.append(ExternalClientsStatuses.RESOURCE_REQUEST_FAILED.value)
            return status_list

        for relation in self.state.external_client_relations:
            if not any(
                f"relation-{relation.id}" in key
                for key in self.state.cluster.external_users_credentials.keys()
            ):
                status_list.append(ExternalClientsStatuses.RESOURCE_REQUEST_FAILED.value)

            if not relation.data[self.state.charm.app].get("endpoints"):
                status_list.append(ExternalClientsStatuses.RESOURCE_REQUEST_FAILED.value)

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
