#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""External clients related event handlers."""

import logging
from typing import TYPE_CHECKING

import ops

from charms.data_platform_libs.v1.data_interfaces import (
    BulkResourcesRequestedEvent,
    RequirerCommonModel,
    ResourceProviderEventHandler,
    ResourceRequestedEvent,
    ValkeyResponseModel,
)
from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyCannotGetPrimaryIPError,
    ValkeyWorkloadCommandError,
)
from literals import EXTERNAL_CLIENTS_RELATION
from statuses import ExternalClientsStatuses

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class ExternalClientsEvents(ops.Object):
    """Handle all events for external client relations."""

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="client_events")
        self.charm = charm

        self.valkey_provides = ResourceProviderEventHandler(
            self.charm,
            EXTERNAL_CLIENTS_RELATION,
            RequirerCommonModel,
            bulk_event=True,
        )

        self.framework.observe(
            self.valkey_provides.on.bulk_resources_requested, self._on_bulk_resources_requested
        )
        self.framework.observe(
            self.valkey_provides.on.resource_requested, self._on_bulk_resources_requested
        )
        self.framework.observe(
            self.charm.on[EXTERNAL_CLIENTS_RELATION].relation_joined,
            self._on_client_relation_joined
        )
        self.framework.observe(
            self.charm.on[EXTERNAL_CLIENTS_RELATION].relation_broken,
            self._on_client_relation_broken,
        )

    def _on_bulk_resources_requested(
        self, event: BulkResourcesRequestedEvent[RequirerCommonModel] | ResourceRequestedEvent
    ) -> None:
        """Handle bulk resources requested event."""
        if not self.charm.unit.is_leader():
            return

        if not self.charm.state.unit_server.model:
            logger.info("Peer relation not ready yet")
            event.defer()
            return

        logger.info("Processing resource request for external client relation")
        try:
            primary_endpoint = self.charm.sentinel_manager.get_primary_endpoint()
            replica_endpoints = self.charm.sentinel_manager.get_replica_endpoints()
            sentinel_endpoints = self.charm.sentinel_manager.get_sentinel_endpoints()
            # will be adjusted once cluster mode is supported
            ha_mode = "sentinel"
            tls_ca = (
                self.charm.workload.read_file(self.charm.workload.tls_paths.client_ca)
                if self.charm.state.unit_server.is_tls_enabled
                else None
            )
            version = self.charm.cluster_manager.get_version()
        except (ValkeyCannotGetPrimaryIPError, ValkeyWorkloadCommandError) as e:
            logger.error("Not ready to process client relation: %s", e)
            event.defer()
            return

        # backward compatibility to data-interfaces v0
        if isinstance(event, ResourceRequestedEvent):
            requests = [event.request]
        else:
            requests = event.requests

        responses = []
        for request in requests:
            username = self.charm.client_manager.get_username(
                event.relation.id, request.request_id
            )
            if not (password := self.charm.client_manager.get_password(username)):
                password = self.charm.config_manager.generate_password()
            self.charm.client_manager.add_managed_user_if_required(
                username, password, request.resource
            )

            response = next(
                (
                    res
                    for res in self.valkey_provides.responses(event.relation, ValkeyResponseModel)
                    if res.request_id == request.request_id
                ),
                None,
            ) or ValkeyResponseModel(
                username=username,
                request_id=request.request_id,
                resource=request.resource,
                salt=request.salt,
            )

            response.username = username
            response.password = password
            response.endpoints = primary_endpoint
            response.read_only_endpoints = replica_endpoints
            response.sentinel_endpoints = sentinel_endpoints
            response.mode = ha_mode
            response.tls = self.charm.state.unit_server.is_tls_enabled
            response.tls_ca = tls_ca
            response.version = version

            responses.append(response)

        logger.info("Updating ACL configuration in Valkey")
        try:
            self.charm.config_manager.set_acl_file()
            self.charm.cluster_manager.reload_acl_file()
        except (ValkeyACLLoadError, ValkeyWorkloadCommandError) as e:
            logger.error(e)
            self.charm.status.set_running_status(
                ExternalClientsStatuses.USER_SETUP_FAILED.value,
                scope="unit",
                statuses_state=self.charm.state.statuses,
                component_name=self.charm.client_manager.name,
            )
            event.defer()
            return

        if responses:
            self.valkey_provides.set_responses(event.relation.id, responses)

        self.charm.state.statuses.delete(
            ExternalClientsStatuses.USER_SETUP_FAILED.value,
            scope="unit",
            component=self.charm.client_manager.name,
        )

    def _on_client_relation_joined(self, event: ops.RelationJoinedEvent) -> None:
        """Handle new client relations on non-leader units."""
        if self.charm.unit.is_leader():
            return

        if not self.charm.client_manager.does_user_exist_for_relation(event.relation.id):
            logger.info("Waiting for managed user to be created")
            event.defer()
            return

        logger.info("Reconciling ACL configuration in Valkey")
        try:
            self.charm.config_manager.set_acl_file()
            self.charm.cluster_manager.reload_acl_file()
        except (ValkeyACLLoadError, ValkeyWorkloadCommandError) as e:
            logger.error(e)
            self.charm.status.set_running_status(
                ExternalClientsStatuses.USER_SETUP_FAILED.value,
                scope="unit",
                statuses_state=self.charm.state.statuses,
                component_name=self.charm.client_manager.name,
            )
            event.defer()
            return

        self.charm.state.statuses.delete(
            ExternalClientsStatuses.USER_SETUP_FAILED.value,
            scope="unit",
            component=self.charm.client_manager.name,
        )

    def _on_client_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Handle the relation-broken event."""
        if not self.charm.state.unit_server.model or self.charm.state.unit_server.is_being_removed:
            return

        if self.charm.unit.is_leader():
            logger.info("Removing managed users for external client relation")
            self.charm.client_manager.remove_managed_users(event.relation.id)

        logger.info("Updating ACL configuration in Valkey")
        try:
            self.charm.config_manager.set_acl_file()
            self.charm.cluster_manager.reload_acl_file()
        except (ValkeyACLLoadError, ValkeyWorkloadCommandError) as e:
            logger.error(e)
            event.defer()
            return
