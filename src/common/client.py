# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

import asyncio
import logging
from typing import Any

from glide import (
    AdvancedGlideClientConfiguration,
    GlideClient,
    GlideClientConfiguration,
    GlideError,
    NodeAddress,
    ServerCredentials,
    TlsAdvancedConfiguration,
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
        tls_cert: bytes | None,
        tls_key: bytes | None,
        tls_ca_cert: bytes | None,
    ):
        self.hosts = hosts
        self.user = username
        self.password = password
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.tls_ca_cert = tls_ca_cert

    async def create_client(self) -> GlideClient:
        """Initialize the Valkey client."""
        addresses = [NodeAddress(host=host, port=CLIENT_PORT) for host in self.hosts]
        credentials = ServerCredentials(username=self.user, password=self.password)

        # only use the TLS options if the client cert is available
        if self.tls_cert:
            tls_config = TlsAdvancedConfiguration(
                client_cert_pem=self.tls_cert,
                client_key_pem=self.tls_key,
                root_pem_cacerts=self.tls_ca_cert,
            )

            client_config = GlideClientConfiguration(
                addresses,
                use_tls=True,
                credentials=credentials,
                request_timeout=1000,  # in milliseconds
                advanced_config=AdvancedGlideClientConfiguration(tls_config=tls_config),
            )
        else:
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
        except (GlideError, FileNotFoundError) as e:
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

    def reload_tls(self, tls_config: dict[str, str]) -> None:
        """Trigger Valkey to load the TLS settings."""
        cmd = ["CONFIG", "SET"]

        # avoid "bind: Address already in use" by advancing the disabled port
        if tls_config["tls-port"] == "0":
            cmd.append("tls-port")
            cmd.append("0")
            tls_config.pop("tls-port")

        for key, value in tls_config.items():
            cmd.append(key)
            cmd.append(value.strip("'"))
        logger.debug("Loading TLS settings: %s", cmd)

        try:
            result = asyncio.run(self._run_custom_command(cmd))
            logger.debug("Loading TLS settings: %s", result)
        except ValkeyCustomCommandError as e:
            logger.error(f"Error loading TLS settings: {e}")
            raise ValkeyTLSLoadError("Could not load TLS settings: %s", e)
