#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Base objects for workload operations across different substrates."""

from abc import ABC, abstractmethod


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
