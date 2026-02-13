# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

import logging
from typing import Literal

from core.base_workload import WorkloadBase
from literals import CLIENT_PORT, SENTINEL_PORT

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
        hostname: str | None = None,
    ) -> tuple[str, str | None]:
        """Execute a Valkey CLI command on the server.

        Args:
            command (list[str]): The CLI command to execute, as a list of arguments.
            hostname (str | None): The hostname to connect to. If None, defaults to the private IP of the unit.

        Returns:
            tuple[str, str | None]: The standard output and standard error from the command execution.

        Raises:
            ValkeyWorkloadCommandError: If the CLI command fails to execute.
        """
        if not hostname:
            hostname = self.workload.get_private_ip()
        port = CLIENT_PORT if self.connect_to == "valkey" else SENTINEL_PORT
        user = self.username
        password = self.password
        cli_command: list[str] = [
            self.workload.cli,
            "-h",
            hostname,
            "-p",
            str(port),
            "--user",
            user,
            "--pass",
            password,
        ] + command
        output, error = self.workload.exec(cli_command)
        return output, error
