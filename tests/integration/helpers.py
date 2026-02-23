#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import contextlib
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import jubilant
import valkey
import yaml
from data_platform_helpers.advanced_statuses.models import StatusObject
from dateutil.parser import parse
from ops import SecretNotFoundError, StatusBase

from literals import (
    CLIENT_PORT,
    INTERNAL_USERS_PASSWORD_CONFIG,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    PEER_RELATION,
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


def create_valkey_client(
    hostname: str,
    username: str | None = CharmUsers.VALKEY_ADMIN.value,
    password: str | None = None,
    tls_enabled: bool = False,
) -> valkey.Valkey:
    """Create and return a Valkey client connected to the cluster.

    Args:
        hostname: The hostname of the Valkey cluster node.
        username: The username for authentication.
        password: The password for the internal user.
        tls_enabled: Whether TLS certificates are needed.

    Returns:
        A Valkey client instance connected to the cluster.
    """
    client = valkey.Valkey(
        host=hostname,
        port=CLIENT_PORT,
        username=username,
        password=password,
        decode_responses=True,
    )
    return client


def create_sentinel_client(
    hostnames: list[str],
    valkey_user: str | None = CharmUsers.VALKEY_ADMIN.value,
    valkey_password: str | None = None,
    sentinel_user: str | None = CharmUsers.SENTINEL_ADMIN.value,
    sentinel_password: str | None = None,
) -> valkey.Sentinel:
    """Create and return a Valkey Sentinel client connected to the cluster.

    Args:
        hostnames: A list of hostnames for the Sentinel nodes.
        valkey_user: The username for authentication to Valkey.
        valkey_password: The password for the internal user for Valkey authentication.
        sentinel_user: The username for authentication to Sentinel.
        sentinel_password: The password for the internal user for Sentinel authentication.

    Returns:
        A Valkey Sentinel client instance connected to the cluster.
    """
    sentinel_client = valkey.Sentinel(
        [(host, 26379) for host in hostnames],
        username=valkey_user,
        password=valkey_password,
        sentinel_kwargs={
            "password": sentinel_password,
            "username": sentinel_user,
        },
        decode_responses=True,
    )
    return sentinel_client


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
    client = await create_valkey_client(
        hostnames=hostnames, username=username, password=password, tls_enabled=tls_enabled
    )
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
    client = await create_valkey_client(
        hostnames=hostnames, username=username, password=password, tls_enabled=tls_enabled
    )
    return await client.get(key)


@contextlib.contextmanager
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
        juju.scp(f"{unit}:{tls_path}/client.pem", "client.pem", container="valkey")
        juju.scp(f"{unit}:{tls_path}/client.key", "client.key", container="valkey")
        juju.scp(f"{unit}:{tls_path}/ca_certs/client_ca.pem", "client_ca.pem", container="valkey")
    else:
        tls_path = "/var/snap/charmed-valkey/current/tls"
        juju.scp(f"{unit}:{tls_path}/client.pem", "client.pem")
        juju.scp(f"{unit}:{tls_path}/client.key", "client.key")
        juju.scp(f"{unit}:{tls_path}/ca_certs/client_ca.pem", "client_ca.pem")


def get_primary_ip(juju: jubilant.Juju, app: str) -> str:
    """Get the primary node of the Valkey cluster.

    Returns:
        The IP address of the primary node.
    """
    hostnames = get_cluster_hostnames(juju, app)
    client = create_sentinel_client(
        hostnames=hostnames,
        valkey_user=CharmUsers.VALKEY_ADMIN.value,
        valkey_password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        sentinel_user=CharmUsers.SENTINEL_CHARM_ADMIN.value,
        sentinel_password=get_password(juju, user=CharmUsers.SENTINEL_CHARM_ADMIN),
    )
    return client.discover_master("primary")[0]


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


def seed_valkey(juju: jubilant.Juju, target_gb: float = 1.0) -> None:
    # Connect to Valkey
    primary_ip = get_primary_ip(juju, APP_NAME)
    client = valkey.Valkey(
        host=primary_ip,
        port=CLIENT_PORT,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )

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

    try:
        while keys_added < total_keys:
            pipe = client.pipeline(transaction=False)

            # Fill the batch
            for i in range(batch_size):
                key_idx = keys_added + i
                pipe.set(f"{SEED_KEY_PREFIX}{key_idx}", random_data)

                if keys_added + i >= total_keys:
                    break

            pipe.execute()
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
        logger.info(f"\nSeeding complete! Added {keys_added:,} keys in {total_time:.2f} seconds.")
