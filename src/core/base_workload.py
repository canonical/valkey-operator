#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Base objects for workload operations across different substrates."""

import logging
import socket
import subprocess
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class WorkloadBase(ABC):
    """Base interface for common workload operations."""

    @property
    @abstractmethod
    def can_connect(self) -> bool:
        """Check if the workload service can be reached."""
        pass

    @abstractmethod
    def start(self) -> None:
        """Start the workload service."""
        pass

    @abstractmethod
    def write_config_file(self, config: dict[str, str]) -> None:
        """Write config properties to the config file on disk.

        Args:
            config (dict): The config properties to be written.
        """
        pass

    @abstractmethod
    def write_file(
        self,
        content: str,
        path: str,
        mode: int | None = None,
        user: str | None = None,
        group: str | None = None,
    ) -> None:
        """Write content to a file on disk.

        Note:
            mode, user, and group are optional parameters used only on k8s workloads.

        Args:
            content (str): The content to be written.
            path (str): The file path where the content should be written.
            mode (int, optional): The file mode (permissions). Defaults to None.
            user (str, optional): The user name. Defaults to None.
            group (str, optional): The group name. Defaults to None.
        """
        pass

    def get_private_ip(self) -> str:
        """Get the Private IP address of the current unit."""
        cmd = "unit-get private-address"
        try:
            output = subprocess.run(
                cmd,
                check=True,
                text=True,
                shell=True,
                capture_output=True,
                timeout=10,
            )
            if output.returncode == 0:
                return output.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Error executing command '{cmd}': {e}")

        return socket.gethostbyname(socket.gethostname())
