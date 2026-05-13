#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Base objects for workload operations across different substrates."""

import logging
from abc import ABC, abstractmethod
from typing import IO, Protocol, runtime_checkable

from charmlibs import pathops

from common.exceptions import ValkeyWorkloadCommandError


@runtime_checkable
class ProcessHandle(Protocol):
    """Streaming subprocess handle returned by ``WorkloadBase.exec_stream``."""

    stdout: IO[bytes]

    def wait(self) -> tuple[int, str]:
        """Wait for completion. Returns ``(returncode, stderr_text)``."""
        ...

    def kill(self) -> None:
        """Terminate the underlying process forcefully."""
        ...

logger = logging.getLogger(__name__)


class TLSPaths:
    """Object to store the TLS paths."""

    def __init__(self, tls_root: pathops.LocalPath | pathops.ContainerPath):
        self.tls_root = tls_root

    @property
    def client_ca(self) -> pathops.LocalPath | pathops.ContainerPath:
        """Path to the client CA."""
        return self.ca_certs_dir / "client_ca.pem"

    @property
    def client_cert(self) -> pathops.LocalPath | pathops.ContainerPath:
        """Path to the client cert."""
        return self.tls_root / "client.pem"

    @property
    def client_key(self) -> pathops.LocalPath | pathops.ContainerPath:
        """Path to the client key."""
        return self.tls_root / "client.key"

    @property
    def ca_certs_dir(self) -> pathops.LocalPath | pathops.ContainerPath:
        """Path to the directory for CA certs."""
        return self.tls_root / "ca_certs"


class WorkloadBase(ABC):
    """Base interface for common workload operations."""

    root_dir: pathops.PathProtocol
    config_file: pathops.PathProtocol
    sentinel_config_file: pathops.PathProtocol
    acl_file: pathops.PathProtocol
    sentinel_acl_file: pathops.PathProtocol
    working_dir: pathops.PathProtocol
    tls_dir: pathops.PathProtocol
    tls_paths: TLSPaths
    valkey_service: str
    sentinel_service: str
    cli: str
    user: str

    @property
    @abstractmethod
    def can_connect(self) -> bool:
        """Check if the workload service can be reached."""
        pass

    @abstractmethod
    def start(self) -> None:
        """Start the workload service.

        Raises:
            ValkeyServicesFailedToStartError: If the service fails to start.
            ValkeyServiceNotAliveError: If the service is not alive after start.
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop the workload service."""
        pass

    def restart(self, service: str) -> None:
        """Restart a workload service."""
        pass

    def exec_stream(self, command: list[str]) -> ProcessHandle:
        """Spawn a command whose stdout streams to the caller as raw bytes.

        Unlike :meth:`exec`, this does not buffer stdout and has no timeout.
        Stderr is captured and returned by ``ProcessHandle.wait()``.
        Used for long-running streaming uploads such as ``valkey-cli --rdb -``.

        Subclasses must override this method.
        """
        raise NotImplementedError("Subclass must implement exec_stream")

    @abstractmethod
    def exec(self, command: list[str]) -> tuple[str, str | None]:
        """Run a command on the workload substrate."""
        pass

    @abstractmethod
    def alive(self) -> bool:
        """Check if the Valkey services are running.

        Returns:
            bool: True if the services are active, False otherwise.
        """
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

    def read_file(self, path: pathops.PathProtocol) -> str:
        """Read a text file and return the string contents.

        Args:
            path (PathProtocol): The file path to be read.
        """
        try:
            return path.read_text()
        except (
            FileNotFoundError,
            PermissionError,
            pathops.PebbleConnectionError,
            UnicodeError,
        ) as e:
            raise ValkeyWorkloadCommandError(e)
