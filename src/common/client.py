# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

from valkey import Valkey

from common.exceptions import ValkeyUserManagementError
from literals import CLIENT_PORT


class ValkeyClient:
    """Handle valkey client connections."""

    def __init__(
        self,
        username: str,
        password: str,
        host: str,
    ):
        self.host = host
        self.user = username
        self.password = password
        self.client = Valkey(port=CLIENT_PORT, username=username, password=password)

    # async def create_client(self) -> GlideClient:
    #     """Initialize the Valkey client."""
    #     addresses = [NodeAddress(host=host, port=CLIENT_PORT) for host in self.host]
    #     credentials = ServerCredentials(self.user, self.password)
    #     client_config = GlideClusterClientConfiguration(
    #         addresses,
    #         credentials=credentials,
    #     )
    #     return await GlideClient.create(client_config)

    def update_password(self, username: str, new_password: str) -> None:
        """Update a user's password.

        Args:
            username (str): The username to update.
            new_password (str): The new password.
        """
        # try:
        #     client = await self.create_client()
        #     await client.custom_command(
        #         [
        #             "ACL",
        #             "SETUSER",
        #             username,
        #             "resetpass",
        #             f">{new_password}",
        #         ]
        #     )
        # except Exception as e:
        #     raise ValkeyUserManagementError(f"Could not update password for user {username}: {e}")
        # finally:
        #     await client.close()
        try:
            self.client.acl_setuser(
                username, enabled=True, reset_passwords=True, passwords=[f"+{new_password}"]
            )
            self.client.acl_save()
        except Exception as e:
            raise ValkeyUserManagementError(f"Could not update password for user {username}: {e}")
