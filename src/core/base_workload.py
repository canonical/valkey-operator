#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Base objects for workload operations across different substrates."""

import logging
from abc import ABC, abstractmethod

from charmlibs import pathops

from common.exceptions import ValkeyWorkloadCommandError

logger = logging.getLogger(__name__)


class WorkloadBase(ABC):
    """Base interface for common workload operations."""

    def __init__(self) -> None:
        """Initialize the WorkloadBase."""
        self.root_dir: pathops.PathProtocol
        self.config_file: pathops.PathProtocol
        self.sentinel_config_file: pathops.PathProtocol
        self.acl_file: pathops.PathProtocol
        self.sentinel_acl_file: pathops.PathProtocol
        self.working_dir: pathops.PathProtocol
        self.cli: str
        self.user: str

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
    def exec(self, command: list[str]) -> tuple[str, str | None]:
        """Run a command on the workload substrate."""
        pass

    @abstractmethod
    def alive(self) -> bool:
        """Check if the Valkey service is running."""
        pass

    def write_file(
        self,
        content: str,
        path: pathops.PathProtocol,
        mode: int | None = None,
        user: str | None = None,
        group: str | None = None,
    ) -> None:
        """Write content to a file on disk.

        Note:
            mode, user, and group are optional parameters used only on k8s workloads.

        Args:
            content (str): The content to be written.
            path (pathops.PathProtocol): The file path where the content should be written.
            mode (int, optional): The file mode (permissions). Defaults to None.
            user (str, optional): The user name. Defaults to None.
            group (str, optional): The group name. Defaults to None.
        """
        try:
            path.write_text(content, mode=mode, user=user, group=group)
        except (
            FileNotFoundError,
            LookupError,
            NotADirectoryError,
            PermissionError,
            pathops.PebbleConnectionError,
            ValueError,
        ) as e:
            raise ValkeyWorkloadCommandError(e)

    def write_config_file(self, config: dict[str, str]) -> None:
        """Write config properties to the config file on disk.

        Args:
            config (dict): The config properties to be written.
        """
        config_string = "\n".join(f"{str(key)}{' '}{str(value)}" for key, value in config.items())

        path = self.config_file
        try:
            path.write_text(config_string, user=self.user, group=self.user)
        except (
            FileNotFoundError,
            LookupError,
            NotADirectoryError,
            PermissionError,
            pathops.PebbleConnectionError,
            ValueError,
        ) as e:
            raise ValkeyWorkloadCommandError(e)
