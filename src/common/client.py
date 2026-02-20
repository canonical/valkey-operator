# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

import logging
from typing import Literal

from tenacity import retry, stop_after_attempt, wait_fixed

from common.exceptions import ValkeyWorkloadCommandError
from core.base_workload import WorkloadBase
from literals import CLIENT_PORT, PRIMARY_NAME, SENTINEL_PORT

logger = logging.getLogger(__name__)


class ValkeyClient:
    """Handle valkey client connections."""

    def __init__(
        self,
        username: str,
        password: str,
        workload: WorkloadBase,
        connect_to: Literal["valkey", "sentinel"] = "valkey",
    ):
        self.username = username
        self.password = password
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
        port = CLIENT_PORT if self.connect_to == "valkey" else SENTINEL_PORT
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
        ] + command
        logger.debug(f"Executing CLI command on {hostname}: {cli_command}")
        output, error = self.workload.exec(cli_command)
        return output.strip(), error

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
                logger.error(
                    "Unexpected output format when getting persistence info from Valkey server at %s: %s",
                    hostname,
                    output,
                )
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
            logger.error(
                "Failed to set key %s on Valkey server at %s: stdout: %s, stderr: %s",
                key,
                hostname,
                output,
                err,
            )
            return False
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to set key {key} on Valkey server at {hostname}: {e}")
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
                "Failed to set config %s on Valkey server at %s: stdout: %s, stderr: %s",
                parameter,
                hostname,
                output,
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
                "Failed to load ACL file on Valkey server at %s: stdout: %s, stderr: %s",
                hostname,
                output,
                err,
            )
            return False
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to load ACL file on Valkey server at {hostname}: {e}")
            return False

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
                logger.error(
                    "Unexpected output format when getting primary IP from sentinel at %s: %s",
                    hostname,
                    output,
                )
                return None
            return output_parts[0]
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to get primary IP from sentinel at {hostname}: {e}")
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
                logger.error(
                    "Unexpected output format when getting master info from sentinel at %s: %s",
                    hostname,
                    output,
                )
                return None
            return {info_parts[i]: info_parts[i + 1] for i in range(0, len(info_parts), 2)}
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to get master info from sentinel at {hostname}: {e}")
            return None

    def sentinel_failover(self, hostname: str):
        """Trigger a failover through the sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if the failover command was executed successfully, False otherwise.
        """
        if not self.connect_to == "sentinel":
            logger.error(
                "Attempted to trigger failover through sentinel while client is configured to connect to valkey."
            )
            raise ValueError("Client is not configured to connect to sentinel.")
        try:
            output, err = self.exec_cli_command(
                command=["sentinel", "failover", PRIMARY_NAME, "coordinated"],
                hostname=hostname,
            )
            if "OK" not in output.strip():
                logger.error(
                    "Failed to trigger failover through sentinel at %s: stdout: %s, stderr: %s",
                    hostname,
                    output,
                    err,
                )
                raise ValkeyWorkloadCommandError(
                    f"Failed to trigger failover through sentinel at {hostname}: stdout, stderr: {(output, err)}"
                )
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to trigger failover through sentinel at {hostname}: {e}")
            raise

    def sentinel_reset_state(self, hostname: str) -> None:
        """Reset the sentinel state for the primary.

        Args:
            hostname (str): The hostname to connect to.
        """
        if not self.connect_to == "sentinel":
            logger.error(
                "Attempted to reset sentinel state through sentinel while client is configured to connect to valkey."
            )
            raise ValueError("Client is not configured to connect to sentinel.")
        try:
            output, err = self.exec_cli_command(
                command=["sentinel", "reset", PRIMARY_NAME],
                hostname=hostname,
            )
            if output != "1":
                raise ValkeyWorkloadCommandError(
                    f"Failed to reset sentinel state through sentinel at {hostname}: stdout, stderr: {(output, err)}"
                )
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to reset sentinel state through sentinel at {hostname}: {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        reraise=True,
    )
    def sentinel_get_replica_info(self, hostname: str) -> str:
        """Get the replicas information of the primary from sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            str | None: The output of the "sentinel replicas" command if retrieved successfully, None otherwise.
        """
        if not self.connect_to == "sentinel":
            logger.error(
                "Attempted to get replica info from sentinel while client is configured to connect to valkey."
            )
            raise ValueError("Client is not configured to connect to sentinel.")
        try:
            output, err = self.exec_cli_command(
                command=["sentinel", "replicas", PRIMARY_NAME],
                hostname=hostname,
            )
            logger.debug(
                "Output of 'sentinel replicas' command from sentinel at %s: stdout, stderr: %s",
                hostname,
                (output, err),
            )
            if not output.strip():
                logger.warning(f"No replica info found in sentinel at {hostname}.")
                raise ValkeyWorkloadCommandError(
                    f"No replica info found in sentinel at {hostname}."
                )
            return output.strip()
        except ValkeyWorkloadCommandError as e:
            logger.error(f"Failed to get replica info from sentinel at {hostname}: {e}")
            raise
