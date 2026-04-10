#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from tests.integration.continuous_writes import ContinuousWrites
from tests.integration.helpers import create_valkey_client, exec_valkey_cli

logger = logging.getLogger(__name__)

# WRITES_LAST_WRITTEN_VAL_PATH = "last_written_value"
# KEY = "cw_key"

KEY = ContinuousWrites.KEY
WRITES_LAST_WRITTEN_VAL_PATH = ContinuousWrites.LAST_WRITTEN_VAL_PATH


def start_continuous_writes(
    endpoints: str,
    valkey_user: str,
    valkey_password: str,
    sentinel_user: str,
    sentinel_password: str,
) -> None:
    """Create a subprocess instance of `continuous writes` and start writing data to valkey."""
    subprocess.Popen(
        [
            "python3",
            "tests/integration/continuous_writes.py",
            endpoints,
            valkey_user,
            valkey_password,
            sentinel_user,
            sentinel_password,
        ]
    )


def stop_continuous_writes() -> None:
    """Shut down the subprocess instance of the `continuous writes`."""
    proc = subprocess.Popen(["pkill", "-15", "-f", "continuous_writes.py"])
    proc.communicate()


async def assert_continuous_writes_increasing(
    hostnames: list[str],
    username: str,
    password: str,
    tls_enabled: bool = False,
) -> None:
    """Assert that the continuous writes are increasing."""
    async with create_valkey_client(
        hostnames,
        username=username,
        password=password,
        tls_enabled=tls_enabled,
    ) as client:
        writes_count = await client.llen(KEY)
        await asyncio.sleep(10)
        more_writes = await client.llen(KEY)
        assert more_writes > writes_count, "Writes not continuing to DB"
        logger.info("Continuous writes are increasing.")


def assert_continuous_writes_consistent(
    hostnames: list[str],
    username: str,
    password: str,
) -> None:
    """Assert that the continuous writes are consistent."""
    last_written_value = int(Path(WRITES_LAST_WRITTEN_VAL_PATH).read_text())

    if not last_written_value:
        raise ValueError("Could not read last written value from file.")

    values: list[int] | None = None

    for endpoint in hostnames:
        current_values: list[int] = json.loads(
            exec_valkey_cli(endpoint, username, password, f"LRANGE {KEY} 0 -1", json=True).stdout
        )
        if values is None:
            values = current_values

        last_value = int(current_values[0]) if current_values else None
        assert last_written_value == last_value, (
            f"endpoint: {endpoint}, expected value: {last_written_value}, current value: {last_value}"
        )
        assert values == current_values, (
            f"endpoint: {endpoint}, expected values: {values}, current values: {current_values}"
        )
