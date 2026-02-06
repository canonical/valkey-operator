#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Base objects for workload operations across different substrates."""

from abc import ABC, abstractmethod

from charmlibs import pathops

from common.exceptions import ValkeyWorkloadCommandError


class TLSPaths:
    """Object to store the TLS paths."""

    def __init__(self, tls_root: pathops.LocalPath or pathops.ContainerPath):
        self.tls_root = tls_root

    @property
    def client_ca(self) -> pathops.LocalPath or pathops.ContainerPath:
        """Path to the client CA."""
        return self.ca_certs_dir / "client_ca.pem"

    @property
    def client_cert(self) -> pathops.LocalPath or pathops.ContainerPath:
        """Path to the client cert."""
        return self.tls_root / "client.pem"

    @property
    def client_key(self) -> pathops.LocalPath or pathops.ContainerPath:
        """Path to the client key."""
        return self.tls_root / "client.key"

    @property
    def peer_ca(self) -> pathops.LocalPath or pathops.ContainerPath:
        """Path to the peer CA."""
        return self.ca_certs_dir / "peer_ca.pem"

    @property
    def peer_cert(self) -> pathops.LocalPath or pathops.ContainerPath:
        """Path to the peer cert."""
        return self.tls_root / "peer.pem"

    @property
    def peer_key(self) -> pathops.LocalPath or pathops.ContainerPath:
        """Path to the peer key."""
        return self.tls_root / "peer.key"

    @property
    def ca_certs_dir(self) -> pathops.LocalPath or pathops.ContainerPath:
        """Path to the directory for CA certs."""
        return self.tls_root / "ca_certs"


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
            path (PathProtocol): The file path where the content should be written.
        """
        try:
            path.write_text(content)
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
            path.write_text(config_string)
        except (
            FileNotFoundError,
            LookupError,
            NotADirectoryError,
            PermissionError,
            pathops.PebbleConnectionError,
            ValueError,
        ) as e:
            raise ValkeyWorkloadCommandError(e)

    def remove_file(self, path: pathops.PathProtocol) -> None:
        """Delete a file on disk.

        Args:
            path (PathProtocol): The file path where the content should be written.
        """
        try:
            path.unlink(missing_ok=True)
        except (
            IsADirectoryError,
            PermissionError,
            pathops.PebbleConnectionError,
        ) as e:
            raise ValkeyWorkloadCommandError(e)
