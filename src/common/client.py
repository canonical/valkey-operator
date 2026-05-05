# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

import json
import logging
from typing import Any

from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

from common.exceptions import ValkeyTLSLoadError, ValkeyWorkloadCommandError
from core.base_workload import WorkloadBase
from literals import CLIENT_PORT, PRIMARY_NAME, SENTINEL_PORT, SENTINEL_TLS_PORT, TLS_PORT

logger = logging.getLogger(__name__)


class CliClient:
    """Handle valkey client connections."""

    port: int = CLIENT_PORT

    def __init__(
        self,
        username: str,
        password: str,
        tls: bool,
        workload: WorkloadBase,
    ):
        self.username = username
        self.password = password
        self.tls = tls
        self.workload = workload

    def exec_cli_command(
        self,
        command: list[str],
        hostname: str,
        json_output: bool = True,
    ) -> Any:
        """Execute a Valkey CLI command on the server.

        Args:
            command (list[str]): The CLI command to execute, as a list of arguments.
            hostname (str): The hostname to connect to.
            json_output (bool): Whether to parse the output as JSON.

        Returns:
            Any: The output from the command execution, parsed as JSON if requested.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute.
        """
        port = self.port
        cli_command: list[str] = [
            self.workload.cli,
            "--no-auth-warning",
            "-h",
            hostname,
            "-p",
            str(port),
            "--user",
            self.username,
            "--pass",
            self.password,
        ] + (["--json"] if json_output else [])

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
        output = output.strip()
        if error:
            logger.error(
                "Error executing CLI command on Valkey server at %s: stderr: %s",
                hostname,
                error,
            )
            raise ValkeyWorkloadCommandError(
                f"Error executing CLI command on Valkey server at {hostname}"
            )

        if json_output:
            try:
                output = json.loads(output)
            except json.JSONDecodeError as e:
                raise ValkeyWorkloadCommandError(
                    f"Failed to parse JSON output from CLI command on Valkey server at {hostname}"
                ) from e
        return output


class ValkeyClient(CliClient):
    """Handle valkey client connections."""

    def __init__(
        self,
        username: str,
        password: str,
        tls: bool,
        workload: WorkloadBase,
    ):
        super().__init__(username, password, tls, workload)
        self.port = TLS_PORT if tls else CLIENT_PORT

    def ping(self, hostname: str) -> bool:
        """Ping the Valkey server to check if it's responsive.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if the server responds to the ping command, False otherwise.
        """
        try:
            return "PONG" in self.exec_cli_command(["ping"], hostname=hostname, json_output=False)
        except ValkeyWorkloadCommandError:
            return False

    def info_persistence(self, hostname: str) -> dict[str, str]:
        """Get the persistence information of the Valkey server.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            dict[str, str]: The persistence information retrieved from the server.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute.
        """
        # command does not have a JSON output format, so we need to parse the raw output
        output = self.exec_cli_command(
            ["info", "persistence"], hostname=hostname, json_output=False
        )
        if not output.strip():
            raise ValkeyWorkloadCommandError("Failed to query persistence info")

        values = {}
        for line in output.strip().splitlines():
            if line.startswith("#"):
                continue
            values_parts = line.split(":", 1)
            values[values_parts[0]] = values_parts[1]
        return values

    def info_server(self, hostname: str) -> dict[str, str]:
        """Get the server information of the Valkey server.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            dict[str, str] The server information retrieved from Valkey.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute.
        """
        output = self.exec_cli_command(["info", "server"], hostname=hostname, json_output=False)
        if not output.strip():
            raise ValkeyWorkloadCommandError("Failed to query server info")

        values = {}
        for line in output.strip().splitlines():
            if line.startswith("#"):
                continue
            values_parts = line.split(":", 1)
            values[values_parts[0]] = values_parts[1]
        return values

    def set(
        self, hostname: str, key: str, value: str, additional_args: list[str] | None = None
    ) -> bool:
        """Set a key-value pair on the Valkey server.

        Args:
            hostname (str): The hostname to connect to.
            key (str): The key to set.
            value (str): The value to set for the key.
            additional_args (list[str]): Additional arguments to include in the CLI command. Default is an empty list.

        Returns:
            bool: True if the command executed successfully, False otherwise.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        if additional_args is None:
            additional_args = []
        return (
            self.exec_cli_command(["set", key, value] + additional_args, hostname=hostname) == "OK"
        )

    def get(self, hostname: str, key: str) -> Any:
        """Get the value of a key from the Valkey server.

        Args:
            hostname (str): The hostname to connect to.
            key (str): The key to retrieve.

        Returns:
            Any: The value of the key if retrieved successfully.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return self.exec_cli_command(["get", key], hostname=hostname)

    def delifeq(self, hostname: str, key: str, value: str) -> str:
        """Delete a key from the Valkey server if it is equal to a specific value.

        Args:
            hostname (str): The hostname to connect to.
            key (str): The key to delete if the value matches.
            value (str): The value to compare against before deleting the key.

        Returns:
            str: The result of the delifeq command.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return self.exec_cli_command(["delifeq", key, value], hostname=hostname, json_output=False)

    def role(self, hostname: str) -> list[str | Any]:
        """Get the role information of the Valkey server.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            list[str | Any]: The role information retrieved from the server.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return self.exec_cli_command(["role"], hostname=hostname)

    def config_set(self, hostname: str, parameter: str, value: str) -> bool:
        """Set a runtime configuration parameter on the Valkey server.

        Args:
            hostname (str): The hostname to connect to.
            parameter (str): The configuration parameter to set.
            value (str): The value to set for the configuration parameter.

        Returns:
            bool: True if the command executed successfully, False otherwise.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return (
            self.exec_cli_command(["config", "set", parameter, value], hostname=hostname) == "OK"
        )

    def acl_load(self, hostname: str) -> bool:
        """Load the ACL file into the Valkey server.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if the ACL file was loaded successfully, False otherwise.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return self.exec_cli_command(["acl", "load"], hostname=hostname) == "OK"

    def reload_tls(self, tls_config: dict[str, str], hostname: str) -> None:
        """Trigger to load the TLS settings."""
        cmd = ["CONFIG", "SET"]

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

    def save(self, hostname: str) -> None:
        """Run a synchronous (blocking) save for the dataset."""
        self.exec_cli_command(["save"], hostname=hostname, json_output=False)


class SentinelClient(CliClient):
    """Handle sentinel-specific client connections."""

    def __init__(
        self,
        username: str,
        password: str,
        tls: bool,
        workload: WorkloadBase,
    ):
        super().__init__(username, password, tls, workload)
        self.port = SENTINEL_TLS_PORT if tls else SENTINEL_PORT

    def ping(self, hostname: str) -> bool:
        """Ping the Sentinel server to check if it's responsive.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if the server responds to the ping command, False otherwise.
        """
        try:
            return "PONG" in self.exec_cli_command(["ping"], hostname=hostname, json_output=False)
        except ValkeyWorkloadCommandError:
            return False

    def get_primary_addr_by_name(self, hostname: str) -> str:
        """Get the primary IP address from the sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            str: The primary IP address if retrieved successfully.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return self.exec_cli_command(
            command=["sentinel", "get-primary-addr-by-name", PRIMARY_NAME], hostname=hostname
        )

    def primary(self, hostname: str) -> dict[str, str]:
        """Get the primary info from the sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            (dict[str, str]): The primary info if retrieved successfully.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return self.exec_cli_command(
            command=["sentinel", "primary", PRIMARY_NAME], hostname=hostname
        )

    def failover_primary_coordinated(self, hostname: str) -> bool:
        """Trigger a failover through the sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if the failover command was executed successfully, False otherwise.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return (
            self.exec_cli_command(
                command=["sentinel", "failover", PRIMARY_NAME, "coordinated"],
                hostname=hostname,
            )
            == "OK"
        )

    # wait for 3m+10 for the failover to finish
    # sentinel waits 3m before timing out a failover
    @retry(
        stop=stop_after_attempt(19),
        wait=wait_fixed(10),
        retry=retry_if_result(lambda in_progress: in_progress),
        retry_error_callback=lambda _: True,
    )
    def is_failover_in_progress(self, hostname: str) -> bool:
        """Check if a failover is in progress through the sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            bool: True if a failover is in progress, False otherwise.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return "failover_in_progress" in self.primary(hostname=hostname).get("flags", "")

    def reset(self, hostname: str) -> None:
        """Reset the sentinel state for the primary.

        Args:
            hostname (str): The hostname to connect to.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output
        """
        self.exec_cli_command(
            command=["sentinel", "reset", PRIMARY_NAME],
            hostname=hostname,
        )

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_fixed(1),
        reraise=True,
    )
    def replicas_primary(self, hostname: str) -> list[dict[str, str]]:
        """Get the replicas information of the primary from sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            (list[dict[str, str]]): The list of replicas with their information.
        """
        return self.exec_cli_command(
            command=["sentinel", "replicas", PRIMARY_NAME], hostname=hostname
        )

    def sentinels_primary(self, hostname: str) -> list[dict[str, str]]:
        """Get the list of sentinels that see the same primary from the sentinel.

        Args:
            hostname (str): The hostname to connect to.

        Returns:
            (list[dict[str, str]]): result of `sentinel sentinels primary` structured into a list of dicts

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return self.exec_cli_command(
            command=["sentinel", "sentinels", PRIMARY_NAME], hostname=hostname
        )

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_fixed(1),
        reraise=True,
    )
    def set(self, hostname: str, *args: str) -> bool:
        """Set a sentinel configuration parameter through the CLI.

        Args:
            hostname (str): The hostname to connect to.
            *args (str): The sentinel configuration parameters to set, as a variable list of strings.

        Returns:
            bool: True if the command executed successfully, False otherwise.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute or returns unexpected output.
        """
        return (
            self.exec_cli_command(
                command=["sentinel", "set"] + list(args),
                hostname=hostname,
            )
            == "OK"
        )
