#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess
import time

import valkey
from tenacity import Retrying, stop_after_attempt, wait_fixed

from literals import CLIENT_PORT, SENTINEL_PORT
from tests.integration.continuous_writes import ContinuousWrites

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


def assert_continuous_writes_increasing(
    endpoints: str,
    valkey_user: str,
    valkey_password: str,
    sentinel_user: str,
    sentinel_password: str,
) -> None:
    """Assert that the continuous writes are increasing."""
    client = valkey.Sentinel(
        [(host, SENTINEL_PORT) for host in endpoints.split(",")],
        username=valkey_user,
        password=valkey_password,
        sentinel_kwargs={"password": sentinel_password, "username": sentinel_user},
    )
    master = client.master_for("primary")
    writes_count = int(master.llen(KEY))
    time.sleep(10)
    more_writes = int(master.llen(KEY))
    assert more_writes > writes_count, "Writes not continuing to DB"
    logger.info("Continuous writes are increasing.")


def assert_continuous_writes_consistent(
    endpoints: str,
    valkey_user: str,
    valkey_password: str,
) -> None:
    """Assert that the continuous writes are consistent."""
    last_written_value = None
    for attempt in Retrying(stop=stop_after_attempt(5), wait=wait_fixed(5)):
        with attempt:
            with open(WRITES_LAST_WRITTEN_VAL_PATH, "r") as f:
                last_written_value = int(f.read().rstrip())

    if not last_written_value:
        raise ValueError("Could not read last written value from file.")

    for endpoint in endpoints.split(","):
        client = valkey.Valkey(
            host=endpoint,
            port=CLIENT_PORT,
            username=valkey_user,
            password=valkey_password,
            decode_responses=True,
        )
        last_value = int(client.lrange(KEY, 0, 0)[0])
        count = int(client.llen(KEY))
        assert last_written_value == last_value, (
            f"endpoint: {endpoint}, expected value: {last_written_value}, current value: {last_value}"
        )
        assert count == last_written_value + 1, (
            f"endpoint: {endpoint}, expected count: {last_written_value + 1}, current count: {count}"
        )
        logger.info(f"Continuous writes are consistent on {endpoint}.")
