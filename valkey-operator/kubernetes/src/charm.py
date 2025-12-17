#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed k8s operator for Valkey."""

import logging

import common.events.base_events as base_events
import ops
from data_platform_helpers.advanced_statuses.handler import StatusHandler

logger = logging.getLogger(__name__)


class EtcdOperatorCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)

        # --- EVENT HANDLERS ---
        self.base_events = base_events