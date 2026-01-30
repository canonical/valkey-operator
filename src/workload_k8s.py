#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Implementation of WorkloadBase for running Valkey on K8s."""

import logging
from typing import override

import ops
from charmlibs import pathops

from common.exceptions import ValkeyExecCommandError
from core.base_workload import WorkloadBase
from literals import CHARM, CHARM_USER, CONFIG_FILE, SENTINEL_CONFIG_FILE

logger = logging.getLogger(__name__)


class ValkeyK8sWorkload(WorkloadBase):
    """Implementation of WorkloadBase for running a Valkey container on K8s."""

    def __init__(self, container: ops.Container | None) -> None:
        if not container:
            raise AttributeError("Container is required.")

        self.container = container
        self.config_file = pathops.ContainerPath(CONFIG_FILE, container=container)
        self.sentinel_config = pathops.ContainerPath(SENTINEL_CONFIG_FILE, container=container)
        self.valkey_service = "valkey"
        self.sentinel_service = "valkey-sentinel"
        self.metric_service = "metric_exporter"

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
                    "command": f"valkey-server {self.config_file}",
                    "user": CHARM_USER,
                    "group": CHARM_USER,
                    "startup": "enabled",
                },
                self.sentinel_service: {
                    "override": "replace",
                    "summary": "Valkey sentinel service",
                    "command": f"valkey-sentinel {self.sentinel_config}",
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
        self.container.restart(self.valkey_service, self.sentinel_service, self.metric_service)

    @override
    def write_config_file(self, config: dict[str, str]) -> None:
        config_string = "\n".join(f"{str(key)}{' '}{str(value)}" for key, value in config.items())

        path = self.config_file
        path.write_text(config_string)

    @override
    def write_file(
        self,
        content: str,
        path: str,
        mode: int | None = None,
        user: str | None = None,
        group: str | None = None,
    ) -> None:
        """Write content to a file on disk.

        Args:
            content (str): The content to be written.
            path (str): The file path where the content should be written.
            mode (int, optional): The file mode (permissions). Defaults to None.
            user (str, optional): The user name. Defaults to None.
            group (str, optional): The group name. Defaults to None.
        """
        file_path = pathops.ContainerPath(path, container=self.container)
        file_path.write_text(content, mode=mode, user=user, group=group)

    def mkdir(
        self, path: str, mode: int = 0o755, user: str | None = None, group: str | None = None
    ) -> None:
        """Create a directory on disk.

        Args:
            path (str): The directory path to be created.
            mode (int, optional): The directory mode (permissions). Defaults to None.
            user (str, optional): The user name. Defaults to None.
            group (str, optional): The group name. Defaults to None.
        """
        dir_path = pathops.ContainerPath(path, container=self.container)
        dir_path.mkdir(mode=mode, user=user, group=group)

    def alive(self) -> bool:
        """Check if the Valkey service is running."""
        for service_name in [
            self.valkey_service,
            self.sentinel_service,
            self.metric_service,
        ]:
            service = self.container.get_service(service_name)
            if not service.is_running():
                return False
        return True

    def exec_command(
        self, command: list[str], username: str, password: str
    ) -> tuple[str, str | None] | None:
        """Execute a Valkey command inside the container.

        Args:
            command (list[str]): The command to execute as a list of strings.
            username (str): The username for authentication.
            password (str): The password for authentication.

        Returns:
            bool: True if the command executed successfully, False otherwise.
        """
        full_command = ["valkey-cli"] + ["--user", username, "--pass", password] + command
        try:
            process = self.container.exec(full_command)
            out, err = process.wait_output()
            if err:
                logger.warning("Command returned error: %s", err)
            return out.strip(), err.strip() if err else None
        except (ops.pebble.ExecError, ops.pebble.ChangeError) as e:
            logger.error("Error executing command: %s", e)
            raise ValkeyExecCommandError(f"Could not execute command{e}")
