#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""External clients related event handlers."""

import logging
import time
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
    ValkeyServicesFailedToStartError,
    ValkeyWorkloadCommandError,
)
from literals import EXTERNAL_CLIENTS_RELATION, PEER_RELATION
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
            self.charm.on[PEER_RELATION].relation_changed,
            self._on_peer_relation_changed,
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

        if not self.charm.state.unit_server.is_started:
            logger.info("Valkey not ready yet")
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
            if self.charm.client_manager.does_username_exist(username):
                logger.info("Request ignored: User already exists. Id: %s", request.request_id)
                continue

            if not (password := self.charm.client_manager.get_password(username)):
                password = self.charm.config_manager.generate_password()
            self.charm.client_manager.add_managed_user(username, password, request.resource)

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

        if not responses:
            logger.info("No updates to process on resource request")
            return

        logger.info("Updating ACL configuration in Valkey")
        try:
            self.charm.config_manager.set_acl_file()
            self.charm.cluster_manager.reload_acl_file()
            self.charm.config_manager.set_sentinel_acl_file()
            # todo: request rolling restart once https://github.com/canonical/valkey-operator/pull/23 is merged
            self.charm.sentinel_manager.restart_service()
        except (
            ValkeyACLLoadError,
            ValkeyServicesFailedToStartError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error(e)
            self.charm.status.set_running_status(
                ExternalClientsStatuses.USER_SETUP_FAILED.value,
                scope="unit",
                statuses_state=self.charm.state.statuses,
                component_name=self.charm.client_manager.name,
            )
            event.defer()
            return

        self.valkey_provides.set_responses(event.relation.id, responses)
        self.charm.state.cluster.update({"client_user_epoch": time.time()})
        self.charm.state.statuses.delete(
            ExternalClientsStatuses.USER_SETUP_FAILED.value,
            scope="unit",
            component=self.charm.client_manager.name,
        )

    def _on_peer_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle peer relation changes in regard to external client relations."""
        if (
            not self.charm.state.unit_server.is_started
            or not self.charm.state.external_client_relations
        ):
            return

        if self.charm.unit.is_leader():
            # this catches all changes from scaling operations, TLS switchover, IP changes, etc.
            try:
                self._update_client_relations()
            except (ValkeyCannotGetPrimaryIPError, ValkeyWorkloadCommandError) as e:
                logger.error("Error updating client relations: %s", e)
                event.defer()
            finally:
                return

        # from here on, the code is only relevant for non-leader units
        if (
            self.charm.state.unit_server.model.client_user_epoch
            >= self.charm.state.cluster.model.client_user_epoch
        ):
            logger.debug("ACLs on this unit up-to-date")
            return

        logger.info("Reconciling ACL configuration in Valkey")
        try:
            self.charm.config_manager.set_acl_file()
            self.charm.cluster_manager.reload_acl_file()
            self.charm.config_manager.set_sentinel_acl_file()
            # todo: request rolling restart once https://github.com/canonical/valkey-operator/pull/23 is merged
            self.charm.sentinel_manager.restart_service()
        except (
            ValkeyACLLoadError,
            ValkeyServicesFailedToStartError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error(e)
            self.charm.status.set_running_status(
                ExternalClientsStatuses.USER_SETUP_FAILED.value,
                scope="unit",
                statuses_state=self.charm.state.statuses,
                component_name=self.charm.client_manager.name,
            )
            event.defer()
            return

        self.charm.state.unit_server.update({"client_user_epoch": time.time()})
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

        if self.charm.client_manager.does_user_exist_for_relation(event.relation.id):
            logger.info("Waiting for managed users to be cleaned up")
            event.defer()
            return

        logger.info("Updating ACL configuration in Valkey")
        try:
            self.charm.config_manager.set_acl_file()
            self.charm.cluster_manager.reload_acl_file()
            self.charm.config_manager.set_sentinel_acl_file()
            # todo: request rolling restart once https://github.com/canonical/valkey-operator/pull/23 is merged
            self.charm.sentinel_manager.restart_service()
        except (
            ValkeyACLLoadError,
            ValkeyServicesFailedToStartError,
            ValkeyWorkloadCommandError,
        ) as e:
            logger.error(e)
            event.defer()
            return

    def _update_client_relations(self) -> None:
        """Update provider data for client relations."""
        logger.info("Updating provider data for client relations")

        primary_endpoints = self.charm.sentinel_manager.get_primary_endpoint()
        replica_endpoints = self.charm.sentinel_manager.get_replica_endpoints()
        sentinel_endpoints = self.charm.sentinel_manager.get_sentinel_endpoints()
        tls_ca = (
            self.charm.workload.read_file(self.charm.workload.tls_paths.client_ca)
            if self.charm.state.unit_server.is_tls_enabled
            else None
        )
        version = self.charm.cluster_manager.get_version()

        for relation in self.charm.state.external_client_relations:
            if not (responses := self.valkey_provides.responses(relation, ValkeyResponseModel)):
                logger.warning("Skipping relation %s with no responses.", relation.id)
                continue

            for request in self.valkey_provides.requests(relation):
                if not (
                    current_response := next(
                        (res for res in responses if res.request_id == request.request_id), None
                    )
                ):
                    logger.warning("Skipping relation %s, no matching response.", relation.id)
                    continue

                current_response.endpoints = primary_endpoints
                current_response.read_only_endpoints = replica_endpoints
                current_response.sentinel_endpoints = sentinel_endpoints
                current_response.tls = self.charm.state.unit_server.is_tls_enabled
                current_response.tls_ca = tls_ca
                current_response.version = version

            self.valkey_provides.set_responses(relation.id, responses)
