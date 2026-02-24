#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, NamedTuple

import jubilant
import yaml
from data_platform_helpers.advanced_statuses.models import StatusObject
from dateutil.parser import parse
from glide import (
    AdvancedGlideClientConfiguration,
    GlideClient,
    GlideClientConfiguration,
    InfoSection,
    NodeAddress,
    ServerCredentials,
    TlsAdvancedConfiguration,
)
from ops import SecretNotFoundError, StatusBase

from literals import (
    CLIENT_PORT,
    INTERNAL_USERS_PASSWORD_CONFIG,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    PEER_RELATION,
    TLS_PORT,
    CharmUsers,
)

logger = logging.getLogger(__name__)


METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME: str = METADATA["name"]
IMAGE_RESOURCE = {"valkey-image": METADATA["resources"]["valkey-image"]["upstream-source"]}
INTERNAL_USERS_SECRET_LABEL = (
    f"{PEER_RELATION}.{APP_NAME}.app.{INTERNAL_USERS_SECRET_LABEL_SUFFIX}"
)
SEED_KEY_PREFIX = "seed:key:"
TLS_NAME = "self-signed-certificates"
TLS_CERT_FILE = "client.pem"
TLS_KEY_FILE = "client.key"
TLS_CA_FILE = "client_ca.pem"


def does_status_match(
    model_status: jubilant.Status,
    expected_unit_statuses: dict[str, List[StatusObject]] | None = None,
    expected_app_statuses: dict[str, List[StatusObject]] | None = None,
    num_units: dict[str, int] | None = None,
) -> bool:
    """Check that current app and/or unit status matches expectation for given apps.

    Args:
        model_status: represents the jubilant model's current status
        expected_unit_statuses: dict mapping app name to list of expected StatusObject for units
        expected_app_statuses: dict mapping app name to its list of expected StatusObject
        num_units: dict mapping app name to expected number of units
    """
    return (
        (
            expected_unit_statuses is None
            or _does_unit_workload_status_match(model_status, expected_unit_statuses)
        )
        and (
            expected_app_statuses is None
            or _does_app_status_match(model_status, expected_app_statuses)
        )
        and (num_units is None or verify_unit_count(model_status, unit_count=num_units))
    )


def _does_unit_workload_status_match(
    model_status: jubilant.Status, expected_statuses: dict[str, List[StatusObject]]
) -> bool:
    """Check that current workload status matches expectation for given apps' units.

    Args:
        model_status: represents the jubilant model's current status
        expected_statuses: dict mapping app names to list of expected StatusObject
    """
    return all(
        all(
            any(
                does_message_match(unit_status.workload_status.message, status)
                for status in expected_status
            )
            for unit_status in model_status.get_units(app).values()
        )
        for app, expected_status in expected_statuses.items()
    )


def _does_app_status_match(
    model_status: jubilant.Status, expected_statuses: dict[str, List[StatusObject]]
) -> bool:
    """Check that current app status matches expectation for given apps.

    Args:
        model_status: represents the jubilant model's current status
        expected_statuses: dict mapping app names to list of expected StatusObject
    """
    return all(
        any(
            does_message_match(model_status.apps.get(app).app_status.message, status)
            for status in expected_status
        )
        for app, expected_status in expected_statuses.items()
    )


def does_message_match(expected_status_message: str, status: StatusObject) -> bool:
    """Check if the status message matches the expected message."""
    try:
        juju_status = StatusBase.from_name(status.status, status.message)
        return (
            expected_status_message == juju_status.message
            or expected_status_message.startswith(juju_status.message)
            or juju_status.message.startswith(f"{expected_status_message:.40}")
            or (
                status.short_message is not None
                and status.short_message in expected_status_message
            )
        )
    except KeyError as e:
        logger.error(f"Error attempting to convert StatusObject to ops.StatusBase: {e}")
        return False


def are_apps_active_and_agents_idle(
    status: jubilant.Status,
    *apps: str,
    idle_period: int = 0,
    unit_count: int | dict[str, int] | None = None,
) -> bool:
    """Check that all given apps are active, their agents idle (optional idle interval too) and optionally verify unit count as well.

    Args:
        status: represents the jubilant model's current status
        apps: A list of applications whose statuses to test against
        idle_period: Seconds to wait for the agents of each application unit to be idle.
        unit_count: The desired number of units to wait for, can be > to 0.
            If set as int, this value is expected for all apps but if more granularity is needed,
            pass a dictionary such as: {"app1": 2, "app2": 1, ...}, if set to -1, the check
            only happens at the application level.
    """
    return (
        jubilant.all_active(status, *apps)
        and jubilant.all_agents_idle(status, *apps)
        and _check_apps_idle_period(status, *apps, idle_period=idle_period)
        and verify_unit_count(status, *apps, unit_count=unit_count)
    )


def are_agents_idle(
    status: jubilant.Status,
    *apps: str,
    idle_period: int = 0,
    unit_count: int | dict[str, int] | None = None,
) -> bool:
    """Check that agents of all given apps are idle (optional idle interval too). Optionally verify unit count as well.

    Args:
        status: represents the jubilant model's current status
        apps: A list of applications whose statuses to test against
        idle_period: Seconds to wait for the agents of each application unit to be idle.
        unit_count: The desired number of units to wait for, should be > 0.
            If set as int, this value is expected for all apps but if more granularity is needed,
            pass a dictionary such as: {"app1": 2, "app2": 1, ...}, if set to -1, the check
            only happens at the application level.
    """
    return (
        jubilant.all_agents_idle(status, *apps)
        and _check_apps_idle_period(status, *apps, idle_period=idle_period)
        and verify_unit_count(status, *apps, unit_count=unit_count)
    )


def _check_apps_idle_period(status: jubilant.Status, *apps: str, idle_period: int) -> bool:
    return all(
        parse(unit.juju_status.since, ignoretz=True) + timedelta(seconds=idle_period)
        < datetime.now()
        for app in apps
        for unit in status.get_units(app).values()
    )


def verify_unit_count(
    status: jubilant.Status, *apps: str, unit_count: int | dict[str, int] | None = None
):
    """Verify the unit count for an application.

    Args:
        status: represents the jubilant model's current status
        apps: A list of applications whose statuses to test against
        unit_count: The desired number of units to wait for, can be >= to -1
            if set as int, this value is expected for all apps but if more granularity is needed,
            pass a dictionary such as: {"app1": 2, "app2": 1, ...}, if set to -1, the check
            only happens at the application level.
    """
    if not unit_count:
        return True

    if isinstance(unit_count, int):
        if unit_count == 0:
            return True
        unit_count = dict.fromkeys(apps, unit_count)
    elif not unit_count:
        unit_count = dict.fromkeys(apps, -1)
    else:
        for app in apps:
            if app not in unit_count:
                unit_count[app] = 1

    return all(count == len(status.get_units(app)) for app, count in unit_count.items())


def get_cluster_hostnames(juju: jubilant.Juju, app_name: str) -> list[str]:
    """Get the hostnames of all units in the Valkey application.

    Args:
        juju: The Juju client instance.
        app_name: The name of the Valkey application.

    Returns:
        A list of hostnames for all units in the Valkey application.
    """
    status = juju.status()
    model_info = juju.show_model()

    if model_info.type == "kubernetes":
        return [unit.address for unit in status.get_units(app_name).values()]

    return [unit.public_address for unit in status.get_units(app_name).values()]


def get_secret_by_label(juju: jubilant.Juju, label: str) -> dict[str, str]:
    for secret in juju.secrets():
        if label == secret.label:
            revealed_secret = juju.show_secret(secret.uri, reveal=True)
            return revealed_secret.content

    raise SecretNotFoundError(f"Secret with label {label} not found")


@asynccontextmanager
async def create_valkey_client(
    hostnames: list[str],
    username: str | None = CharmUsers.VALKEY_ADMIN.value,
    password: str | None = None,
    tls_enabled: bool = False,
):
    """Create and return a Valkey client connected to the cluster.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        username: The username for authentication.
        password: The password for the internal user.
        tls_enabled: Whether TLS certificates are needed.

    Returns:
        A Valkey client instance connected to the cluster.
    """
    addresses = [
        NodeAddress(host=host, port=TLS_PORT if tls_enabled else CLIENT_PORT) for host in hostnames
    ]

    credentials = None
    if username or password:
        credentials = ServerCredentials(username=username, password=password)

    if tls_enabled:
        # Read locally stored certificate files
        with open("client.pem", "rb") as f:
            tls_cert = f.read()
        with open("client.key", "rb") as f:
            tls_key = f.read()
        with open("client_ca.pem", "rb") as f:
            tls_ca_cert = f.read()

    tls_config = TlsAdvancedConfiguration(
        client_cert_pem=tls_cert if tls_enabled else None,
        client_key_pem=tls_key if tls_enabled else None,
        root_pem_cacerts=tls_ca_cert if tls_enabled else None,
    )

    client_config = GlideClientConfiguration(
        addresses,
        credentials=credentials,
        use_tls=True if tls_enabled else False,
        advanced_config=AdvancedGlideClientConfiguration(tls_config=tls_config),
    )

    client = await GlideClient.create(client_config)
    try:
        yield client
    finally:
        await client.close()


def set_password(
    juju: jubilant.Juju,
    password: str,
    username: str = CharmUsers.VALKEY_ADMIN.value,
    application: str = APP_NAME,
) -> None:
    """Set a user password (or update it if existing) via secret.

    Args:
        juju: An instance of Jubilant's Juju class on which to run Juju commands
        password: password to use
        username: the user to set the password
        application: the application the created secret will be granted to
    """
    secret_name = "system_users_secret"

    # if secret exists, update it, else add secret
    existing = next((s for s in juju.secrets() if s.name == secret_name), None)
    if existing:
        juju.update_secret(identifier=existing.uri, content={username: password})
        secret_id = existing.uri
    else:
        secret_id = juju.add_secret(name=secret_name, content={username: password})

    # grant the application access to this secret
    juju.grant_secret(identifier=secret_id, app=application)

    # update the application config to include the secret
    juju.config(app=application, values={INTERNAL_USERS_PASSWORD_CONFIG: secret_id})


@contextmanager
def fast_forward(juju: jubilant.Juju):
    """Context manager that temporarily speeds up update-status hooks to fire every 10s."""
    old = juju.model_config()["update-status-hook-interval"]
    juju.model_config({"update-status-hook-interval": "10s"})
    try:
        yield
    finally:
        juju.model_config({"update-status-hook-interval": old})


def download_client_certificate_from_unit(juju: jubilant.Juju, app_name: str = APP_NAME) -> None:
    """Copy the client certificate files from a unit to the host's filesystem."""
    unit = next(iter(juju.status().get_units(app_name)))
    model_info = juju.show_model()

    if model_info.type == "kubernetes":
        tls_path = "/var/lib/valkey/tls"
        juju.scp(f"{unit}:{tls_path}/{TLS_CERT_FILE}", TLS_CERT_FILE, container="valkey")
        juju.scp(f"{unit}:{tls_path}/{TLS_KEY_FILE}", TLS_KEY_FILE, container="valkey")
        juju.scp(f"{unit}:{tls_path}/ca_certs/{TLS_CA_FILE}", TLS_CA_FILE, container="valkey")
    else:
        tls_path = "/var/snap/charmed-valkey/current/tls"
        juju.scp(f"{unit}:{tls_path}/{TLS_CERT_FILE}", TLS_CERT_FILE)
        juju.scp(f"{unit}:{tls_path}/{TLS_KEY_FILE}", TLS_KEY_FILE)
        juju.scp(f"{unit}:{tls_path}/ca_certs/{TLS_CA_FILE}", TLS_CA_FILE)


async def get_primary_ip(juju: jubilant.Juju, app: str) -> str:
    """Get the primary node of the Valkey cluster.

    Returns:
        The IP address of the primary node.
    """
    hostnames = get_cluster_hostnames(juju, app)
    async with create_valkey_client([hostnames[0]], password=get_password(juju)) as client:
        info = await client.custom_command(["client", "info"])
    match = re.search(r"laddr=([\d\.]+):", info.decode())
    if match:
        return match.group(1)
    raise RuntimeError("Primary IP not found in client info output")


def get_password(juju: jubilant.Juju, user: CharmUsers = CharmUsers.VALKEY_ADMIN) -> str:
    """Retrieve the password for a given internal user from Juju secrets.

    Args:
        juju: The Juju client instance.
        user: The internal user whose password to retrieve.

    Returns:
        The password for the specified internal user.
    """
    secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    return secret.get(f"{user.value}-password", "")


async def seed_valkey(juju: jubilant.Juju, target_gb: float = 1.0) -> None:
    # Connect to Valkey
    hostnames = get_cluster_hostnames(juju, APP_NAME)

    # Configuration
    value_size_bytes = 1024  # 1KB per value
    batch_size = 5000  # Commands per pipeline
    total_bytes_target = target_gb * 1024 * 1024 * 1024
    total_keys = total_bytes_target // value_size_bytes

    logger.debug(
        f"Targeting ~{target_gb}GB ({total_keys:,} keys of {value_size_bytes} bytes each)"
    )

    start_time = time.time()
    keys_added = 0

    # Generate a fixed random block to reuse (saves CPU cycles on generation)
    random_data = os.urandom(value_size_bytes).hex()[:value_size_bytes]
    async with create_valkey_client(hostnames, password=get_password(juju)) as client:
        try:
            while keys_added < total_keys:
                data = {
                    f"{SEED_KEY_PREFIX}{key_idx}": random_data
                    for key_idx in range(keys_added, keys_added + batch_size)
                }

                if await client.mset(data) != "OK":
                    raise RuntimeError("Failed to set data in Valkey cluster")

                keys_added += batch_size

                # Progress reporting
                elapsed = time.time() - start_time
                percent = (keys_added / total_keys) * 100
                logger.info(
                    f"Progress: {percent:.1f}% | Keys: {keys_added:,} | Elapsed: {elapsed:.1f}s",
                )

        except Exception as e:
            logger.error(f"\nError: {e}")
        finally:
            total_time = time.time() - start_time
            logger.info(
                f"\nSeeding complete! Added {keys_added:,} keys in {total_time:.2f} seconds."
            )


valkey_cli_result = NamedTuple(
    "ValkeyCliResult", [("stdout", str), ("stderr", str), ("returncode", int)]
)


def exec_valkey_cli(
    hostname: str, username: str, password: str, command: str
) -> valkey_cli_result:
    """Execute a Valkey CLI command and returns the output as a string."""
    command = (
        f"valkey-cli -h {hostname} -p {CLIENT_PORT} --user {username} --pass {password} {command}"
    )
    result = subprocess.run(
        command.split(), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return valkey_cli_result(
        stdout=result.stdout.strip(), stderr=result.stderr.strip(), returncode=result.returncode
    )


async def set_key(
    hostnames: list[str],
    username: str,
    password: str,
    key: str,
    value: str,
    tls_enabled: bool = False,
) -> bytes | None:
    """Write a key-value pair to the Valkey cluster.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        key: The key to write.
        value: The value to write.
        username: The username for authentication.
        password: The password for authentication.
        tls_enabled: Whether TLS certificates are needed.
    """
    async with create_valkey_client(
        hostnames=hostnames, username=username, password=password, tls_enabled=tls_enabled
    ) as client:
        return await client.set(key, value)


async def get_key(
    hostnames: list[str],
    username: str,
    password: str,
    key: str,
    tls_enabled: bool = False,
) -> bytes | None:
    """Read a value from the Valkey cluster by key.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        key: The key to read.
        username: The username for authentication.
        password: The password for authentication.
        tls_enabled: Whether TLS certificates are needed.
    """
    async with create_valkey_client(
        hostnames=hostnames, username=username, password=password, tls_enabled=tls_enabled
    ) as client:
        return await client.get(key)


def ping(
    hostname: str,
    username: str,
    password: str,
) -> bool:
    """Ping a Valkey cluster node.

    Args:
        hostname: The hostname of the Valkey cluster node.
        username: The username for authentication.
        password: The password for authentication.

    Returns:
        True if the node responds to a ping, False otherwise.
    """
    return exec_valkey_cli(hostname, username, password, "ping").stdout == "PONG"


async def ping_cluster(
    hostnames: list[str],
    username: str,
    password: str,
) -> bool:
    """Ping all nodes in the Valkey cluster.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        username: The username for authentication.
        password: The password for authentication.

    Returns:
        True if all nodes respond to a ping, False otherwise.
    """
    async with create_valkey_client(
        hostnames=hostnames, username=username, password=password
    ) as client:
        return await client.ping() == "PONG".encode()


async def get_number_connected_slaves(
    hostnames: list[str],
    username: str,
    password: str,
) -> int:
    """Get the number of connected slaves in the Valkey cluster.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        username: The username for authentication.
        password: The password for authentication.

    Returns:
        The number of connected slaves.
    """
    async with create_valkey_client(
        hostnames=hostnames, username=username, password=password
    ) as client:
        info = (await client.info([InfoSection.REPLICATION])).decode()
    search_result = re.search(r"connected_slaves:([\d+])", info)
    if not search_result:
        raise ValueError("Could not parse number of connected slaves from info output")
    return int(search_result.group(1))


class NoAuthError(Exception):
    """Raised when authentication fails due to missing credentials."""


class WrongPassError(Exception):
    """Raised when authentication fails due to incorrect credentials."""


async def auth_test(hostnames: list[str], username: str | None, password: str | None) -> bool:
    """Test authentication to the Valkey cluster by attempting to ping it.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        username: The username for authentication.
        password: The password for authentication.

    Returns:
        True if authentication is successful and the cluster responds to a ping, False otherwise.
    """
    try:
        async with create_valkey_client(
            hostnames=hostnames, username=username, password=password
        ) as client:
            return await client.ping() == "PONG".encode()
    except Exception as e:
        error_message = str(e)
        if "NOAUTH" in error_message:
            raise NoAuthError("Authentication failed: NOAUTH error") from e
        elif "WRONGPASS" in error_message:
            raise WrongPassError("Authentication failed: WRONGPASS error") from e
        else:
            raise e
