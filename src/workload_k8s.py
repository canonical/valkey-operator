#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Implementation of WorkloadBase for running Valkey on K8s."""

import logging
from typing import override

import ops
from charmlibs import pathops

from core.base_workload import WorkloadBase
from literals import CHARM, CHARM_USER, CONFIG_FILE

logger = logging.getLogger(__name__)


class ValkeyK8sWorkload(WorkloadBase):
    """Implementation of WorkloadBase for running a Valkey container on K8s."""

    def __init__(self, container: ops.Container | None) -> None:
        if not container:
            raise AttributeError("Container is required.")

        self.container = container
        self.config_file = pathops.ContainerPath(CONFIG_FILE, container=container)
        self.valkey_service = "valkey"
        self.metric_service = "metric_exporter"

    @override
    @property
    def can_connect(self) -> bool:
        return self.container.can_connect()

    @property
    def pebble_layer(self) -> ops.pebble.Layer:
        """Create the Pebble configuration layer for Valkey."""
        layer_config: ops.pebble.LayerDict = {
            "summary": "Valkey layer",
            "description": "Valkey layer",
            "services": {
                self.valkey_service: {
                    "override": "replace",
                    "summary": "Valkey service",
                    "command": f"valkey-server {self.config_file}",
                    "user": CHARM_USER,
                    "group": CHARM_USER,
                    "startup": "enabled",
                },
                self.metric_service: {
                    "override": "replace",
                    "summary": "Valkey metric exporter",
                    "command": "bin/redis_exporter",
                    "user": CHARM_USER,
                    "group": CHARM_USER,
                    "startup": "enabled",
                },
            },
        }
        return ops.pebble.Layer(layer_config)

    @override
    def start(self) -> None:
        self.container.add_layer(CHARM, self.pebble_layer, combine=True)
        self.container.restart(self.valkey_service, self.metric_service)

    @override
    def write_config_file(self, config: dict[str, str]) -> None:
        config_string = "\n".join(f"{str(key)}{' '}{str(value)}" for key, value in config.items())

        path = self.config_file
        path.write_text(config_string)
