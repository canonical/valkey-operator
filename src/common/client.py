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
        # TODO add back when we enable cluster mode
        # client_config = GlideClusterClientConfiguration(
        #     addresses,
        #     credentials=credentials,
        # )
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
            logger.error(f"Error running command {' '.join(command)}: {e}")
            raise ValkeyUserManagementError(f"Could not run command {' '.join(command)}: {e}")
        finally:
            if client:
                await client.close()

    def update_password(self, username: str, new_password: str) -> None:
        """Update a user's password.

        Args:
            username (str): The username to update.
            new_password (str): The new password.
        """
        try:
            result = asyncio.run(
                self._run_custom_command(
                    [
                        "ACL",
                        "SETUSER",
                        username,
                        "resetpass",
                        f">{new_password}",
                    ]
                )
            )

            logger.debug(f"Password update result: {result}")
        except Exception as e:
            logger.error(f"Error updating password for user {username}: {e}")
            raise ValkeyUserManagementError(f"Could not update password for user {username}: {e}")

    def save_acl(self) -> None:
        """Save ACL content to the Valkey server.

        Args:
            acl_content (str): The ACL content to save.
        """
        try:
            result = asyncio.run(self._run_custom_command(["ACL", "SAVE"]))
            logger.debug(f"ACL save result: {result}")
        except Exception as e:
            logger.error(f"Error saving ACL: {e}")
            raise ValkeyUserManagementError(f"Could not save ACL: {e}")
