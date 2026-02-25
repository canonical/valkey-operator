# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

import logging
from typing import Literal

from common.exceptions import ValkeyTLSLoadError, ValkeyWorkloadCommandError
from core.base_workload import WorkloadBase
from literals import CLIENT_PORT, PRIMARY_NAME, SENTINEL_PORT, TLS_PORT

logger = logging.getLogger(__name__)


class ValkeyClient:
    """Handle valkey client connections."""

    def __init__(
        self,
        username: str,
        password: str,
        tls: bool,
        workload: WorkloadBase,
        connect_to: Literal["valkey", "sentinel"] = "valkey",
    ):
        self.username = username
        self.password = password
        self.tls = tls
        self.workload = workload
        self.connect_to = connect_to

    def exec_cli_command(
        self,
        command: list[str],
        hostname: str,
    ) -> tuple[str, str | None]:
        """Execute a Valkey CLI command on the server.

        Args:
            command (list[str]): The CLI command to execute, as a list of arguments.
            hostname (str): The hostname to connect to.

        Returns:
            tuple[str, str | None]: The standard output and standard error from the command execution.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute.
        """
        valkey_port = TLS_PORT if self.tls else CLIENT_PORT
        port = valkey_port if self.connect_to == "valkey" else SENTINEL_PORT

        cli_command: list[str] = [
            self.workload.cli,
            "-h",
            hostname,
            "-p",
            str(port),
            "--user",
            self.username,
            "--pass",
            self.password,
        ]

        if self.tls:
            cli_command.append("--tls")
            cli_command.append("--cert")
            cli_command.append(self.workload.tls_paths.client_cert.as_posix())
            cli_command.append("--key")
            cli_command.append(self.workload.tls_paths.client_key.as_posix())
            cli_command.append("--cacertdir")
            cli_command.append(self.workload.tls_paths.ca_certs_dir.as_posix())

        cli_command = cli_command + command
        output, error = self.workload.exec(cli_command)
        return output, error

    def ping(self, hostname: str) -> bool:
        """Ping the Valkey server to check if it's responsive.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if the server responds to the ping command, False otherwise.
        """
        try:
            output, _ = self.exec_cli_command(["ping"], hostname=hostname)
            return "PONG" in output
        except ValkeyWorkloadCommandError:
            return False

    def get_persistence_info(self, hostname: str) -> dict[str, str] | None:
        """Get the persistence information of the Valkey server.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            dict[str, str] | None: The persistence information retrieved from the server.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute.
        """
        output, _ = self.exec_cli_command(["info", "persistence"], hostname=hostname)
        values = {}
        if not output.strip():
            logger.warning(f"No persistence info found on Valkey server at {hostname}.")
            return None
        for line in output.strip().splitlines():
            if line.startswith("#"):
                continue
            values_parts = line.split(":", 1)
            if len(values_parts) != 2:
                logger.error("Failed to get persistence info from Valkey server")
                return None
            values[values_parts[0]] = values_parts[1]
        return values

    def set_value(self, hostname: str, key: str, value: str) -> bool:
        """Set a key-value pair on the Valkey server.

        Args:
            hostname (str): The hostname to connect to.
            key (str): The key to set.
            value (str): The value to set for the key.

        Returns:
            bool: True if the command executed successfully, False otherwise.
        """
        try:
            output, err = self.exec_cli_command(["set", key, value], hostname=hostname)
            if output.strip() == "OK":
                return True
            logger.error("Failed to set key %s on Valkey server at %s: %s", key, hostname, err)
            return False
        except ValkeyWorkloadCommandError as e:
            logger.error("Failed to set key %s on Valkey server at %s: %s", key, hostname, e)
            return False

    def is_replica_synced(self, hostname: str) -> bool:
        """Check if the replica is synced with the primary.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if the replica is synced with the primary, False otherwise.
        """
        try:
            output, _ = self.exec_cli_command(["role"], hostname=hostname)
            output_parts = output.strip().split()
            return (
                bool(output_parts)
                and output_parts[0] == "slave"
                and output_parts[3] == "connected"
            )
        except ValkeyWorkloadCommandError:
            logger.warning(
                "Could not determine replica sync status from Valkey server at %s.", hostname
            )
            return False

    def config_set(self, hostname: str, parameter: str, value: str) -> bool:
        """Set a runtime configuration parameter on the Valkey server.

        Args:
            hostname (str): The hostname to connect to.
            parameter (str): The configuration parameter to set.
            value (str): The value to set for the configuration parameter.

        Returns:
            bool: True if the command executed successfully, False otherwise.
        """
        try:
            output, err = self.exec_cli_command(
                ["config", "set", parameter, value], hostname=hostname
            )
            if output.strip() == "OK":
                return True
            logger.error(
                "Failed to set config %s on Valkey server at %s: stderr: %s",
                parameter,
                hostname,
                err,
            )
            return False
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to set config {parameter} on Valkey server at {hostname}: {e}")
            return False

    def load_acl(self, hostname: str) -> bool:
        """Load the ACL file into the Valkey server.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if the ACL file was loaded successfully, False otherwise.
        """
        try:
            output, err = self.exec_cli_command(["acl", "load"], hostname=hostname)
            if output.strip() == "OK":
                return True
            logger.error(
                "Failed to load ACL file on Valkey server at %s: stderr: %s",
                hostname,
                err,
            )
            return False
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to load ACL file on Valkey server at {hostname}: {e}")
            return False

    def reload_tls(self, tls_config: dict[str, str], hostname: str) -> None:
        """Trigger to load the TLS settings."""
        cmd = []

        if self.connect_to == "sentinel":
            cmd.append("SENTINEL")

        cmd.append("CONFIG")
        cmd.append("SET")

        for key, value in tls_config.items():
            cmd.append(key)
            cmd.append(value)
        logger.debug("Loading TLS settings: %s", cmd)

        try:
            result = self.exec_cli_command(command=cmd, hostname=hostname)
            logger.debug("Loading TLS settings: %s", result)
        except ValkeyWorkloadCommandError:
            logger.error("Error loading TLS settings")
            raise ValkeyTLSLoadError("Could not load TLS settings")

    def sentinel_get_primary_ip(self, hostname: str) -> str | None:
        """Get the primary IP address from the sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            str | None: The primary IP address if retrieved successfully, None otherwise.
        """
        if not self.connect_to == "sentinel":
            logger.error(
                "Attempted to get primary IP from sentinel while client is configured to connect to valkey."
            )
            raise ValueError("Client is not configured to connect to sentinel.")
        try:
            output, _ = self.exec_cli_command(
                command=["sentinel", "get-master-addr-by-name", PRIMARY_NAME], hostname=hostname
            )
            output_parts = output.strip().split()
            if len(output_parts) != 2:
                logger.error("Failed to get primary IP from sentinel")
                return None
            return output_parts[0]
        except ValkeyWorkloadCommandError:
            logger.error("Failed to get primary IP from sentinel at %s", hostname)
            return None

    def sentinel_get_master_info(self, hostname: str) -> dict[str, str] | None:
        """Get the master info from the sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            dict[str, str] | None: The master info if retrieved successfully, None otherwise.
        """
        if not self.connect_to == "sentinel":
            logger.error(
                "Attempted to get master info from sentinel while client is configured to connect to valkey."
            )
            raise ValueError("Client is not configured to connect to sentinel.")
        try:
            output, _ = self.exec_cli_command(
                command=["sentinel", "master", PRIMARY_NAME], hostname=hostname
            )
            if not output.strip():
                logger.warning(f"No master info found in sentinel at {hostname}.")
                return None
            info_parts = output.strip().split()
            if len(info_parts) % 2 != 0:
                logger.error("Failed to get master info from sentinel")
                return None
            return {info_parts[i]: info_parts[i + 1] for i in range(0, len(info_parts), 2)}
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to get master info from sentinel at {hostname}: {e}")
            return None
