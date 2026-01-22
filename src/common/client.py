# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

import asyncio
import logging
from typing import Any

from glide import (
    GlideClient,
    GlideClientConfiguration,
    NodeAddress,
    ServerCredentials,
)

from common.exceptions import ValkeyUserManagementError
from literals import CLIENT_PORT

logger = logging.getLogger(__name__)


class ValkeyClient:
    """Handle valkey client connections."""

    def __init__(
        self,
        username: str,
        password: str,
        hosts: list[str],
    ):
        self.hosts = hosts
        self.user = username
        self.password = password

    async def create_client(self) -> GlideClient:
        """Initialize the Valkey client."""
        addresses = [NodeAddress(host=host, port=CLIENT_PORT) for host in self.hosts]
        credentials = ServerCredentials(username=self.user, password=self.password)
        client_config = GlideClientConfiguration(
            addresses,
            credentials=credentials,
            request_timeout=1000,  # in milliseconds
        )
        return await GlideClient.create(client_config)

    async def _run_custom_command(self, command: list[str]) -> Any:
        """Run a custom command on the Valkey client.

        Args:
            command (list[str]): The command to run as a list of strings.

        Returns:
            Any result from the command.
        """
        client = None
        try:
            client = await self.create_client()
            result = await asyncio.wait_for(client.custom_command(command), timeout=5)
            return result
        except Exception as e:
            logger.error("Error running custom command: %s", e)
            raise ValkeyUserManagementError(f"Could not run custom command: {e}")
        finally:
            if client:
                await client.close()

    def load_acl(self) -> None:
        """Load ACL content to the Valkey server."""
        try:
            result = asyncio.run(self._run_custom_command(["ACL", "LOAD"]))
            logger.debug(f"ACL load result: {result}")
        except Exception as e:
            logger.error(f"Error loading ACL: {e}")
            raise ValkeyUserManagementError(f"Could not load ACL: {e}")
