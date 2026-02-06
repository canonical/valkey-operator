#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Implementation of WorkloadBase for running Valkey on K8s."""

import logging
from typing import override

import ops
from charmlibs import pathops

from common.exceptions import ValkeyWorkloadCommandError
from core.base_workload import TLSPaths, WorkloadBase
from literals import ACL_FILE, CHARM, CHARM_USER, CONFIG_FILE

logger = logging.getLogger(__name__)


class ValkeyK8sWorkload(WorkloadBase):
    """Implementation of WorkloadBase for running a Valkey container on K8s."""

    def __init__(self, container: ops.Container | None) -> None:
        if not container:
            raise AttributeError("Container is required.")

        self.container = container
        self.valkey_service = "valkey"
        self.metric_service = "metric_exporter"
        self.root = pathops.ContainerPath("/", container=self.container)
        self.config_file = self.root / CONFIG_FILE
        self.acl_file = self.root / ACL_FILE
        # todo: update this path once directories in the rock are complying with the standard
        self.working_dir = self.root / "var/lib/valkey"
        self.tls_dir = self.root / "var/lib/valkey/tls"
        self.tls_paths: TLSPaths = TLSPaths(tls_root=self.tls_dir)

    @property
    @override
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
                    "command": f"valkey-server {self.config_file.as_posix()}",
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
    def exec(self, command: list[str]) -> str:
        try:
            process = self.container.exec(
                command=command,
                combine_stderr=True,
            )
            output, _ = process.wait_output()
            return output
        except ops.pebble.ExecError as e:
            logger.error("Command failed with %s, %s", e.exit_code, e.stdout)
            raise ValkeyWorkloadCommandError(e)
