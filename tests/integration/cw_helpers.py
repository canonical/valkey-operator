#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import json
import logging
from pathlib import Path
from typing import NamedTuple

import jubilant

from literals import CLIENT_PORT, TLS_PORT, Substrate
from tests.integration.conftest import GLIDE_RUNNER_NAME
from tests.integration.helpers import (
    APP_NAME,
    TLS_CA_FILE,
    TLS_CERT_FILE,
    TLS_KEY_FILE,
    CharmUsers,
    download_client_certificate_from_unit,
    exec_valkey_cli,
    get_cluster_addresses,
    get_password,
)

logger = logging.getLogger(__name__)


class ContinuousWritesStats(NamedTuple):
    last_written_value: int
    total_count: int


KEY = "cw_key"


def configure_cw_runner(
    juju: jubilant.Juju,
    app: str = GLIDE_RUNNER_NAME,
    valkey_app: str = APP_NAME,
    tls_enabled: bool = False,
    substrate: Substrate = Substrate.VM,
) -> None:
    """Configure the continuous writes runner charm to connect to Valkey via config options.

    Endpoints and the admin password are fetched automatically from the Juju
    model. When ``tls_enabled`` is True, client certificates are downloaded
    from a Valkey unit and passed as base64-encoded strings.

    Args:
        juju: Juju client instance.
        app: Name of the continuous writes runner charm application to configure.
        valkey_app: Name of the Valkey application to fetch endpoints from.
        tls_enabled: Whether TLS is enabled.
        substrate: The substrate type (VM or Kubernetes).
    """
    if substrate == Substrate.VM:
        addresses = get_cluster_addresses(juju, valkey_app)
    else:
        # for k8s we construct the hostname
        addresses = [
            unit_name.replace("/", "-") + "." + valkey_app + "-endpoints"
            for unit_name in juju.status().get_units(valkey_app)
        ]

    port = TLS_PORT if tls_enabled else CLIENT_PORT
    endpoints = ",".join(f"{h}:{port}" for h in addresses)
    password = get_password(juju, user=CharmUsers.VALKEY_ADMIN)

    cacert = cert = key = ""
    if tls_enabled:
        download_client_certificate_from_unit(juju, app_name=valkey_app)
        cacert = base64.b64encode(Path(TLS_CA_FILE).read_bytes()).decode()
        cert = base64.b64encode(Path(TLS_CERT_FILE).read_bytes()).decode()
        key = base64.b64encode(Path(TLS_KEY_FILE).read_bytes()).decode()

    glide_config = json.dumps(
        {
            "endpoints": endpoints,
            "username": CharmUsers.VALKEY_ADMIN.value,
            "password": password,
            "tls_enabled": tls_enabled,
            "cacert": cacert,
            "cert": cert,
            "key": key,
        }
    )
    juju.config(app=app, values={"glide-config": glide_config})


def start_continuous_writes(
    juju: jubilant.Juju,
    unit: str = f"{GLIDE_RUNNER_NAME}/leader",
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
        params={"sleep-interval": sleep_interval, "clear-existing": clear},
    )
    assert result.results.get("ok"), f"start-continuous-writes failed: {result}"
    pid = int(result.results["pid"])
    logger.info("Continuous-writes daemon started on %s with PID %d", unit, pid)
    return pid


def stop_continuous_writes(
    juju: jubilant.Juju, unit: str = f"{GLIDE_RUNNER_NAME}/leader"
) -> ContinuousWritesStats:
    """Trigger the stop-continuous-writes action and return write statistics.

    Args:
        juju: Juju client instance.
        unit: Unit name to run the action on.

    Returns:
        ``ContinuousWritesStats`` with ``last_written_value`` (last integer
        successfully written to Valkey) and ``total_count`` (number of items
        in the list).
    """
    result = juju.run(unit, "stop-continuous-writes")
    assert result.results.get("ok"), f"stop-continuous-writes failed: {result}"
    stats = ContinuousWritesStats(
        last_written_value=int(result.results["last-written-value"]),
        total_count=int(result.results["count"]),
    )
    logger.info(
        "Continuous-writes stopped on %s — last_written=%d, count=%d",
        unit,
        stats.last_written_value,
        stats.total_count,
    )
    return stats


def assert_continuous_writes_increasing(
    juju: jubilant.Juju,
    unit: str = f"{GLIDE_RUNNER_NAME}/leader",
    wait: float = 10.0,
) -> None:
    """Run the assert-continuous-writes-increasing action on the requirer charm unit.

    Args:
        juju: Juju client instance.
        unit: Unit name to run the action on.
        wait: Seconds to wait between state samples inside the charm.
    """
    result = juju.run(unit, "assert-continuous-writes-increasing", {"wait": wait})
    assert result.status == "completed" and result.results.get("ok"), (
        f"assert-continuous-writes-increasing failed: {result}"
    )
    logger.info(
        "Continuous writes are increasing on %s (count %s -> %s)",
        unit,
        result.results.get("count-before"),
        result.results.get("count-after"),
    )


def clear_continuous_writes(juju: jubilant.Juju, unit: str) -> None:
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


def assert_continuous_writes_consistent(
    hostnames: list[str],
    username: str,
    password: str,
    last_written_value: int,
    tls_enabled: bool = False,
) -> None:
    """Assert consistency of continuous-writes data across all Valkey instances.

    Checks two properties:
    - The head of the list on every replica matches ``last_written_value``.
    - Every replica holds an identical copy of the list.

    Args:
        hostnames: List of Valkey hostnames to check.
        username: Valkey username.
        password: Valkey password.
        last_written_value: Last integer successfully written, from ``stop_continuous_writes``.
    """
    reference: list[int] | None = None

    for endpoint in hostnames:
        current_values: list[int] = json.loads(
            exec_valkey_cli(
                endpoint,
                username,
                password,
                f"LRANGE {KEY} 0 -1",
                json=True,
                tls_enabled=tls_enabled,
            ).stdout
        )

        last_value = int(current_values[0]) if current_values else None
        assert last_value == last_written_value, (
            f"endpoint {endpoint}: head of list is {last_value}, "
            f"expected last_written_value={last_written_value}"
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
