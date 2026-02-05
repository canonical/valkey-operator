#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""TLS related event handlers."""

import logging
from typing import TYPE_CHECKING

import ops

from literals import PEER_TLS_RELATION_NAME

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


class TLSEvents(ops.Object):
    """Handle all TLS related events."""

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="tls_events")
        self.charm = charm

        for relation in [PEER_TLS_RELATION_NAME]:
            self.framework.observe(
                self.charm.on[relation].relation_created, self._on_relation_created
            )

    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Handle the `relation-created` event."""
        if event.relation.name == PEER_TLS_RELATION_NAME:
            # self.charm.tls_manager.set_tls_state(state=TLSState.TO_TLS, tls_type=TLSType.PEER)
            logger.info(f"peer TLS file: {self.charm.workload.tls.peer_cert.as_posix()}")
