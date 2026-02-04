#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Base objects for workload operations across different substrates."""

from abc import ABC, abstractmethod

from charmlibs import pathops


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
    def exec(self, command: list[str]) -> str:
        """Run a command on the workload substrate."""
        pass

    def write_file(self, content: str, path: pathops.PathProtocol) -> None:
        """Write content to a file on disk.

        Args:
            content (str): The content to be written.
            path (str): The file path where the content should be written.
        """
        path.write_text(content)

    def write_config_file(self, config: dict[str, str]) -> None:
        """Write config properties to the config file on disk.

        Args:
            config (dict): The config properties to be written.
        """
        config_string = "\n".join(f"{str(key)}{' '}{str(value)}" for key, value in config.items())

        path = self.config_file
        path.write_text(config_string)
