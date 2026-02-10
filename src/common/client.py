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

from common.exceptions import (
    ValkeyACLLoadError,
    ValkeyCustomCommandError,
    ValkeyTLSLoadError,
)
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
        # TODO refine exception handling
        except Exception as e:
            logger.error("Error running custom command: %s", e)
            raise ValkeyCustomCommandError(f"Could not run custom command: {e}")
        finally:
            if client:
                await client.close()

    def reload_acl(self) -> None:
        """Load ACL content to the Valkey server."""
        try:
            result = asyncio.run(self._run_custom_command(["ACL", "LOAD"]))
            logger.debug(f"ACL load result: {result}")
        except ValkeyCustomCommandError as e:
            logger.error(f"Error loading ACL: {e}")
            raise ValkeyACLLoadError(f"Could not load ACL: {e}")

    def enable_tls(self, tls_config: dict[str, str]) -> None:
        """Trigger Valkey to load the TLS settings."""
        cmd = ["CONFIG", "SET"]
        for key, value in tls_config.items():
            cmd.append(key)
            cmd.append(value)
        logger.debug("Enabling TLS settings: %s", cmd)

        try:
            result = asyncio.run(self._run_custom_command(cmd))
            logger.debug("Enabled TLS settings: %s", result)
        except ValkeyCustomCommandError as e:
            logger.error(f"Error enabling TLS settings: {e}")
            raise ValkeyTLSLoadError("Could not load TLS settings: %s", e)

    def reload_tls(self) -> None:
        """Trigger Valkey to reload the TLS settings."""
        try:
            cmd = ["CONFIG", "SET", "tls-port", str(CLIENT_PORT)]
            result = asyncio.run(self._run_custom_command(cmd))
            logger.debug("Reload TLS settings: %s", result)
        except ValkeyCustomCommandError as e:
            logger.error(f"Error reloading TLS settings: {e}")
            raise ValkeyTLSLoadError("Could not load TLS settings: %s", e)

    def disable_tls(self) -> None:
        """Trigger Valkey to discard the TLS settings."""
        try:
            cmd = ["CONFIG", "SET", "tls-port", "0", "port", str(CLIENT_PORT)]
            result = asyncio.run(self._run_custom_command(cmd))
            logger.debug("Disable TLS on default port: %s", result)
        except ValkeyCustomCommandError as e:
            logger.error(f"Error disabling TLS settings: {e}")
            raise ValkeyTLSLoadError("Could not disable TLS settings: %s", e)
