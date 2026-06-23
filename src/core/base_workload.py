#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Base objects for workload operations across different substrates."""

import logging
import os
import pathlib
from abc import ABC, abstractmethod
from typing import IO, Protocol, runtime_checkable

from charmlibs import pathops

from common.exceptions import ValkeyWorkloadCommandError
from literals import BACKUP_CA_FILENAME


@runtime_checkable
class ProcessHandle(Protocol):
    """Streaming subprocess handle returned by ``WorkloadBase.exec_stream``.

    Wraps a long-running child process whose stdout is streamed to the
    caller rather than buffered in memory -- ``exec_stream`` is used for
    transfers (e.g. ``valkey-cli --rdb -``) that can exceed the charm
    container's heap.

    Two substrate implementations exist: ``_VmProcessHandle`` over
    ``subprocess.Popen`` and ``_K8sProcessHandle`` over Pebble exec. They
    honour the same contract; substrate-specific behaviour the caller can
    rely on is documented per-method below.

    ``stdout`` is the raw stdout pipe -- read it incrementally; it is never
    fully buffered.
    """

    stdout: IO[bytes]

    def wait(self) -> tuple[int, str]:
        """Block until the process exits; return ``(returncode, stderr_text)``.

        ``returncode`` is the child's exit status, or a negative sentinel
        when the substrate could not determine it (e.g. a Pebble change
        error on K8s). ``stderr_text`` is decoded best-effort and may be
        truncated to a bounded tail -- it is for diagnostics, not parsing.
        """
        ...

    def kill(self) -> None:
        """Best-effort forced termination of the process.

        Safe to call after the process has already exited. Errors are
        logged, not raised, so callers can invoke it unconditionally on
        cleanup paths.
        """
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
    def backup_ca_path(self) -> pathlib.Path:
        """Path to the S3 endpoint CA bundle used by the backup client.

        Deliberately a charm-process-local path, NOT a ``tls_paths`` entry:
        boto3 runs in the charm process, not the workload container, so on
        K8s the two do not share a filesystem and the bundle could not live
        in the workload's (container) TLS dir. Keeping it out of that dir
        also stops the S3 endpoint CA being trusted as a Valkey client CA.
        ``JUJU_CHARM_DIR`` is the charm dir Juju exports for every hook.
        """
        return pathlib.Path(os.environ["JUJU_CHARM_DIR"]) / BACKUP_CA_FILENAME

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

    def exec_stream(self, command: list[str], env: dict[str, str] | None = None) -> ProcessHandle:
        """Spawn a command whose stdout streams to the caller as raw bytes.

        Unlike :meth:`exec`, this does not buffer stdout and has no timeout.
        Stderr is captured and returned by ``ProcessHandle.wait()``.
        Used for long-running streaming uploads such as ``valkey-cli --rdb -``.

        ``env`` entries are added to the process environment; use it for
        secrets (e.g. ``VALKEYCLI_AUTH``) that must not appear on argv.

        Subclasses must override this method.
        """
        raise NotImplementedError("Subclass must implement exec_stream")

    @abstractmethod
    def exec(
        self, command: list[str], env: dict[str, str] | None = None
    ) -> tuple[str, str | None]:
        """Run a command on the workload substrate.

        ``env`` entries are added to the process environment; use it for
        secrets that must not appear on the command line.
        """
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
