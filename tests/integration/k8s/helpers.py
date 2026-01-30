#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import contextlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import jubilant
import yaml
from data_platform_helpers.advanced_statuses.models import StatusObject
from dateutil.parser import parse
from glide import GlideClient, GlideClientConfiguration, NodeAddress, ServerCredentials
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
    return [unit.address for unit in status.get_units(app_name).values()]


def get_secret_by_label(juju: jubilant.Juju, label: str) -> dict[str, str]:
    for secret in juju.secrets():
        if label == secret.label:
            revealed_secret = juju.show_secret(secret.uri, reveal=True)
            return revealed_secret.content

    raise SecretNotFoundError(f"Secret with label {label} not found")


async def create_valkey_client(
    hostnames: list[str],
    username: str | None = CharmUsers.VALKEY_ADMIN.value,
    password: str | None = None,
):
    """Create and return a Valkey client connected to the cluster.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        username: The username for authentication.
        password: The password for the internal user.

    Returns:
        A Valkey client instance connected to the cluster.
    """
    addresses = [NodeAddress(host=host, port=CLIENT_PORT) for host in hostnames]

    credentials = None
    if username or password:
        credentials = ServerCredentials(username=username, password=password)
    client_config = GlideClientConfiguration(
        addresses,
        credentials=credentials,
    )
    return await GlideClient.create(client_config)


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
    hostnames: list[str], username: str, password: str, key: str, value: str
) -> bytes | None:
    """Write a key-value pair to the Valkey cluster.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        key: The key to write.
        value: The value to write.
        username: The username for authentication.
        password: The password for authentication.
    """
    client = await create_valkey_client(hostnames=hostnames, username=username, password=password)
    return await client.set(key, value)


async def get_key(hostnames: list[str], username: str, password: str, key: str) -> bytes | None:
    """Read a value from the Valkey cluster by key.

    Args:
        hostnames: List of hostnames of the Valkey cluster nodes.
        key: The key to read.
        username: The username for authentication.
        password: The password for authentication.
    """
    client = await create_valkey_client(hostnames=hostnames, username=username, password=password)
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
