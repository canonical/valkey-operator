# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

import asyncio
import logging

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
        )
        return await GlideClient.create(client_config)

    def update_password(self, username: str, new_password: str) -> None:
        """Update a user's password.

        Args:
            username (str): The username to update.
            new_password (str): The new password.
        """
        client = None
        try:
            client = asyncio.run(self.create_client())
            result = asyncio.run(
                client.custom_command(
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
        finally:
            if client:
                asyncio.run(client.close())
