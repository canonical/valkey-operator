# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""ValkeyClient utility class to connect to valkey servers."""

import json
import logging

from glide import (
    AdvancedGlideClientConfiguration,
    GlideClient,
    GlideClientConfiguration,
    NodeAddress,
    ServerCredentials,
    TlsAdvancedConfiguration,
)

logger = logging.getLogger(__name__)


class ValkeyClient:
    """Handle valkey client connections."""

    def __init__(
        self,
        username: str,
        password: str | None,
        endpoints: list[str],
        tls_cert: bytes | None,
        tls_key: bytes | None,
        tls_ca_cert: bytes | None,
    ):
        self.endpoints = endpoints
        self.user = username
        self.password = password
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.tls_ca_cert = tls_ca_cert

    async def create_client(self) -> GlideClient:
        """Initialize the Valkey client."""
        addresses = [
            NodeAddress(host, int(port_str))
            for endpoint in self.endpoints
            for host, port_str in [endpoint.rsplit(":", 1)]
        ]

        tls_config = TlsAdvancedConfiguration(
            client_cert_pem=self.tls_cert if self.tls_cert else None,
            client_key_pem=self.tls_key if self.tls_cert else None,
            root_pem_cacerts=self.tls_ca_cert if self.tls_cert else None,
        )

        client_config = GlideClientConfiguration(
            addresses,
            use_tls=True if self.tls_cert else False,
            credentials=ServerCredentials(username=self.user, password=self.password),
            request_timeout=1000,  # in milliseconds
            advanced_config=AdvancedGlideClientConfiguration(tls_config=tls_config),
        )

        return await GlideClient.create(client_config)

    async def set_key(self, key: str, value: str) -> None:
        """Write a key to the Valkey database."""
        client = await self.create_client()

        try:
            await client.set(key, value)
            logger.info("Write to Valkey successful")
        finally:
            await client.close()

    async def get_key(self, key: str) -> str:
        """Retrieve a key from the Valkey database."""
        client = await self.create_client()

        try:
            value = await client.get(key)
            return value.decode()
        finally:
            await client.close()

    async def execute_command(self, args: list[str]) -> str:
        """Execute an arbitrary Valkey command and return the result as a string."""
        client = await self.create_client()

        try:
            result = await client.custom_command(args)
            str_result = ""
            if result is None:
                str_result = ""
            elif isinstance(result, bytes):
                str_result = result.decode()
            elif isinstance(result, list):
                # Decode bytes in lists (e.g. from LRANGE) to return a JSON-serializable structure
                str_result = [
                    item.decode() if isinstance(item, bytes) else item for item in result
                ]
            else:
                str_result = str(result)  # Fallback to string conversion for other types

            return json.dumps(
                str_result
            )  # For other result types, return a JSON string representation
        finally:
            await client.close()
