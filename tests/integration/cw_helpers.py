#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import base64
import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import jubilant

from tests.integration.continuous_writes import ContinuousWrites
from tests.integration.helpers import (
    APP_NAME,
    TLS_CA_FILE,
    TLS_CERT_FILE,
    TLS_KEY_FILE,
    CharmUsers,
    create_valkey_client,
    download_client_certificate_from_unit,
    exec_valkey_cli,
    get_cluster_hostnames,
    get_password,
)

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
    """Create a subprocess instance of continuous writes and start writing data to Valkey.

    Args:
        endpoints: Comma-separated list of Valkey endpoints.
        valkey_user: Valkey username.
        valkey_password: Valkey password.
        sentinel_user: Sentinel username.
        sentinel_password: Sentinel password.
    """
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
    """Shut down the subprocess instance of the continuous writes."""
    proc = subprocess.Popen(["pkill", "-15", "-f", "continuous_writes.py"])
    proc.communicate()


async def assert_continuous_writes_increasing(
    hostnames: list[str],
    username: str,
    password: str,
    tls_enabled: bool = False,
) -> None:
    """Assert that the continuous writes are increasing.

    Args:
        hostnames: List of Valkey hostnames to connect to.
        username: Valkey username.
        password: Valkey password.
        tls_enabled: Whether TLS is enabled.
    """
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


def configure_requirer_charm(
    juju: jubilant.Juju,
    app: str,
    valkey_app: str = APP_NAME,
    tls_enabled: bool = False,
) -> None:
    """Configure the requirer charm to connect to Valkey via config options.

    Endpoints and the admin password are fetched automatically from the Juju
    model. When ``tls_enabled`` is True, client certificates are downloaded
    from a Valkey unit and passed as base64-encoded strings.

    Args:
        juju: Juju client instance.
        app: Name of the requirer charm application to configure.
        valkey_app: Name of the Valkey application to fetch endpoints from.
        tls_enabled: Whether TLS is enabled.
    """
    hostnames = get_cluster_hostnames(juju, valkey_app)
    endpoints = ",".join(f"{h}:6379" for h in hostnames)
    password = get_password(juju, user=CharmUsers.VALKEY_ADMIN)

    cacert = cert = key = ""
    if tls_enabled:
        download_client_certificate_from_unit(juju, app_name=valkey_app)
        cacert = base64.b64encode(Path(TLS_CA_FILE).read_bytes()).decode()
        cert = base64.b64encode(Path(TLS_CERT_FILE).read_bytes()).decode()
        key = base64.b64encode(Path(TLS_KEY_FILE).read_bytes()).decode()

    values: dict = {
        "connection-source": "config",
        "endpoints": endpoints,
        "username": CharmUsers.VALKEY_ADMIN.value,
        "password": password,
        "tls-enabled": tls_enabled,
        "cacert": cacert,
        "cert": cert,
        "key": key,
    }
    juju.config(app=app, values=values)


def start_charm_continuous_writes(
    juju: jubilant.Juju,
    unit: str,
    sleep_interval: float = 1.0,
    config: dict | None = None,
    clear: bool = True,
) -> int:
    """Trigger the start-continuous-writes action on the requirer charm unit.

    Connection info is taken from the Valkey relation by default. To use
    config options instead, pass a ``config`` dict; the options are applied
    to the application before the action runs.

    Args:
        juju: Juju client instance.
        unit: Unit name (e.g. ``"requirer-charm/0"``).
        sleep_interval: Seconds to sleep between writes.
        config: Optional charm config values to set before starting.
        clear: Delete any existing list values before starting.

    Returns:
        PID of the spawned continuous-writes daemon.
    """
    if config:
        app = unit.split("/")[0]
        juju.config(app=app, values=config)

    result = juju.run(
        unit,
        "start-continuous-writes",
        {"sleep-interval": sleep_interval, "clear-existing": clear},
    )
    assert result.results.get("ok"), f"start-continuous-writes failed: {result}"
    pid = int(result.results["pid"])
    logger.info("Continuous-writes daemon started on %s with PID %d", unit, pid)
    return pid


def stop_charm_continuous_writes(juju: jubilant.Juju, unit: str) -> SimpleNamespace:
    """Trigger the stop-continuous-writes action and return write statistics.

    Args:
        juju: Juju client instance.
        unit: Unit name to run the action on.

    Returns:
        Namespace with ``last_written_value`` (last integer successfully
        written to Valkey) and ``count`` (number of items in the list).
    """
    result = juju.run(unit, "stop-continuous-writes")
    assert result.results.get("ok"), f"stop-continuous-writes failed: {result}"
    stats = SimpleNamespace(
        last_written_value=int(result.results["last-written-value"]),
        count=int(result.results["count"]),
    )
    logger.info(
        "Continuous-writes stopped on %s — last_written=%d, count=%d",
        unit,
        stats.last_written_value,
        stats.count,
    )
    return stats


def clear_charm_continuous_writes(juju: jubilant.Juju, unit: str) -> None:
    """Trigger the clear-continuous-writes action on the requirer charm unit.

    Deletes the continuous-writes key from Valkey. Can be called while the
    daemon is stopped to reset data between test runs.

    Args:
        juju: Juju client instance.
        unit: Unit name to run the action on.
    """
    result = juju.run(unit, "clear-continuous-writes")
    assert result.results.get("ok"), f"clear-continuous-writes failed: {result}"
    logger.info("Continuous-writes data cleared on %s", unit)


def assert_charm_continuous_writes_consistent(
    hostnames: list[str],
    username: str,
    password: str,
    stats: SimpleNamespace,
) -> None:
    """Assert consistency of continuous-writes data across all Valkey instances.

    Checks two properties:
    - The head of the list on every replica matches ``stats.last_written_value``.
    - Every replica holds an identical copy of the list.

    Args:
        hostnames: List of Valkey hostnames to check.
        username: Valkey username.
        password: Valkey password.
        stats: Write statistics returned by ``stop_charm_continuous_writes``.
    """
    reference: list[int] | None = None

    for endpoint in hostnames:
        current_values: list[int] = json.loads(
            exec_valkey_cli(endpoint, username, password, f"LRANGE {KEY} 0 -1", json=True).stdout
        )

        last_value = int(current_values[0]) if current_values else None
        assert last_value == stats.last_written_value, (
            f"endpoint {endpoint}: head of list is {last_value}, "
            f"expected last_written_value={stats.last_written_value}"
        )

        if reference is None:
            reference = current_values
        assert current_values == reference, (
            f"endpoint {endpoint}: list diverges from reference.\n"
            f"  reference (first endpoint): {reference[:10]}...\n"
            f"  this endpoint:              {current_values[:10]}..."
        )

    logger.info(
        "Consistency check passed across %d endpoints (list len=%d).",
        len(hostnames),
        len(reference or []),
    )


def assert_continuous_writes_consistent(
    hostnames: list[str],
    username: str,
    password: str,
) -> None:
    """Assert that the continuous writes are consistent.

    Args:
        hostnames: List of Valkey hostnames to check.
        username: Valkey username.
        password: Valkey password.
    """
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
