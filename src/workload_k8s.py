#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Implementation of WorkloadBase for running Valkey on K8s."""

import logging
from typing import override

import ops
from charmlibs import pathops
from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

from common.exceptions import (
    ValkeyServiceNotAliveError,
    ValkeyServicesCouldNotBeStoppedError,
    ValkeyServicesFailedToStartError,
    ValkeyWorkloadCommandError,
)
from core.base_workload import WorkloadBase
from literals import (
    ACL_FILE,
    CHARM,
    CONFIG_FILE,
    SENTINEL_ACL_FILE,
    SENTINEL_CONFIG_FILE,
)

logger = logging.getLogger(__name__)


class ValkeyK8sWorkload(WorkloadBase):
    """Implementation of WorkloadBase for running a Valkey container on K8s."""

    def __init__(self, container: ops.Container | None) -> None:
        if not container:
            raise AttributeError("Container is required.")

        self.container = container
        self.root_dir = pathops.ContainerPath("/", container=self.container)
        self.config_file = self.root_dir / CONFIG_FILE
        self.sentinel_config_file = self.root_dir / SENTINEL_CONFIG_FILE
        self.acl_file = self.root_dir / ACL_FILE
        self.sentinel_acl_file = self.root_dir / SENTINEL_ACL_FILE
        # todo: update this path once directories in the rock are complying with the standard
        self.working_dir = self.root_dir / "var/lib/valkey"
        self.valkey_service = "valkey"
        self.sentinel_service = "valkey-sentinel"
        self.metric_service = "metric_exporter"
        self.cli = "valkey-cli"
        self.user = "_daemon_"

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
                    "user": self.user,
                    "group": self.user,
                    "startup": "enabled",
                },
                self.sentinel_service: {
                    "override": "replace",
                    "summary": "Valkey sentinel service",
                    "command": f"valkey-sentinel {self.sentinel_config_file.as_posix()}",
                    "user": self.user,
                    "group": self.user,
                    "startup": "enabled",
                },
                self.metric_service: {
                    "override": "replace",
                    "summary": "Valkey metric exporter",
                    "command": "bin/redis_exporter",
                    "user": self.user,
                    "group": self.user,
                    "startup": "enabled",
                },
            },
        }
        return ops.pebble.Layer(layer_config)

    @override
    def start(self) -> None:
        try:
            self.container.add_layer(CHARM, self.pebble_layer, combine=True)
            self.container.restart(self.valkey_service, self.sentinel_service, self.metric_service)
        except ops.pebble.ChangeError as e:
            raise ValkeyServicesFailedToStartError(f"Failed to start Valkey services: {e}") from e
        if not self.alive():
            raise ValkeyServiceNotAliveError("Valkey service is not alive after start.")

    @override
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_result(lambda healthy: not healthy),
        retry_error_callback=lambda _: False,
    )
    def alive(self) -> bool:
        for service_name in [
            self.valkey_service,
            self.sentinel_service,
            self.metric_service,
        ]:
            service = self.container.get_service(service_name)
            if not service.is_running():
                return False
        return True

    @override
    def exec(self, command: list[str]) -> tuple[str, str | None]:
        try:
            process = self.container.exec(
                command=command,
            )
            return process.wait_output()
        except ops.pebble.ExecError as e:
            logger.error("Command failed with %s, %s", e.exit_code, e.stdout)
            raise ValkeyWorkloadCommandError(e)

    @override
    def stop(self) -> None:
        try:
            self.container.stop(self.valkey_service, self.sentinel_service, self.metric_service)
        except ops.pebble.ChangeError as e:
            logger.error("Failed to stop Valkey services: %s", e)
            raise ValkeyServicesCouldNotBeStoppedError(
                f"Failed to stop Valkey services: {e}"
            ) from e
