#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
import pytest
from valkey import AuthenticationError

from literals import (
    INTERNAL_USERS_PASSWORD_CONFIG,
    CharmUsers,
)
from statuses import CharmStatuses, ClusterStatuses

from .helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
    create_valkey_client,
    does_status_match,
    fast_forward,
    get_cluster_hostnames,
    get_password,
    get_primary_ip,
    set_password,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"


@pytest.mark.abort_on_fail
def test_build_and_deploy(charm: str, juju: jubilant.Juju) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(charm, resources=IMAGE_RESOURCE, num_units=NUM_UNITS)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )


@pytest.mark.abort_on_fail
async def test_authentication(juju: jubilant.Juju) -> None:
    """Assert that we can authenticate to valkey."""
    primary = get_primary_ip(juju, APP_NAME)
    hostnames = get_cluster_hostnames(juju, APP_NAME)

    # try without authentication
    with pytest.raises(AuthenticationError):
        unauth_client = create_valkey_client(hostname=primary, username=None, password=None)
        await unauth_client.ping()

    # Authenticate with internal user
    password = get_password(juju, user=CharmUsers.VALKEY_ADMIN)
    assert password is not None, "Admin password secret not found"

    for hostname in hostnames:
        client = create_valkey_client(hostname=hostname, password=password)
        assert client.ping() is True, (
            f"Authentication to Valkey cluster failed for host {hostname}"
        )


@pytest.mark.abort_on_fail
async def test_update_admin_password(juju: jubilant.Juju) -> None:
    """Assert the admin password is updated when adding a user secret to the config."""
    # create a user secret and grant it to the application
    new_password = "some-password"
    set_password(juju, new_password)

    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=10),
        timeout=1200,
    )
    primary = get_primary_ip(juju, APP_NAME)
    client = create_valkey_client(
        hostname=primary, username=CharmUsers.VALKEY_ADMIN.value, password=new_password
    )
    assert client.ping() is True, "Failed to authenticate with new admin password"

    assert client.set(TEST_KEY, TEST_VALUE) is True, (
        "Failed to write data after admin password update"
    )

    # update the config again and remove the option `admin-password`
    juju.config(app=APP_NAME, reset=[INTERNAL_USERS_PASSWORD_CONFIG])

    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=10),
        timeout=1200,
    )

    for hostname in get_cluster_hostnames(juju, APP_NAME):
        client = create_valkey_client(
            hostname=hostname, username=CharmUsers.VALKEY_ADMIN.value, password=new_password
        )
        assert client.ping() is True, (
            f"Failed to authenticate with admin password after removing user secret on host {hostname}"
        )
        assert client.get(TEST_KEY) == bytes(TEST_VALUE, "utf-8"), (
            f"Failed to read data after admin password update on host {hostname}"
        )


@pytest.mark.abort_on_fail
async def test_update_admin_password_wrong_username(juju: jubilant.Juju) -> None:
    """Assert the admin password is updated when adding a user secret to the config."""
    # create a user secret and grant it to the application
    new_password = "some-password"
    set_password(juju, username="wrong-username", password=new_password)

    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_app_statuses={APP_NAME: [ClusterStatuses.PASSWORD_UPDATE_FAILED.value]},
        ),
        timeout=1200,
    )

    set_password(juju, username=CharmUsers.VALKEY_ADMIN.value, password=new_password)
    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=10),
        timeout=1200,
    )

    # perform read operation with the updated password
    primary = get_primary_ip(juju, APP_NAME)
    client = create_valkey_client(
        hostname=primary, username=CharmUsers.VALKEY_ADMIN.value, password=new_password
    )
    assert client.ping() is True, "Failed to authenticate with new admin password"
    assert client.set(TEST_KEY, TEST_VALUE) is True, (
        "Failed to write data after admin password update"
    )


@pytest.mark.abort_on_fail
async def test_user_secret_permissions(juju: jubilant.Juju) -> None:
    """If a user secret is not granted, ensure we can process updated permissions."""
    logger.info("Creating new user secret")
    secret_name = "my_secret"
    new_password = "even-newer-password"
    secret_id = juju.add_secret(
        name=secret_name, content={CharmUsers.VALKEY_ADMIN.value: new_password}
    )

    logger.info("Updating configuration with the new secret - but without access")
    juju.config(app=APP_NAME, values={INTERNAL_USERS_PASSWORD_CONFIG: secret_id})

    juju.wait(
        lambda status: does_status_match(
            status,
            expected_app_statuses={APP_NAME: [CharmStatuses.SECRET_ACCESS_ERROR.value]},
        ),
        timeout=1200,
    )

    logger.info("Secret access will be granted now - wait for updated password")
    # deferred `config_changed` event will be retried before `update_status`
    with fast_forward(juju):
        juju.grant_secret(identifier=secret_name, app=APP_NAME)
        juju.wait(
            lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=10),
            timeout=1200,
        )

    # perform read operation with the updated password
    hostnames = get_cluster_hostnames(juju, APP_NAME)
    primary = get_primary_ip(juju, APP_NAME)
    client = create_valkey_client(
        hostname=primary, username=CharmUsers.VALKEY_ADMIN.value, password=new_password
    )
    assert client.ping() is True, "Failed to authenticate with new admin password"
    assert client.set(TEST_KEY, TEST_VALUE) is True, (
        "Failed to write data after admin password update"
    )
    for hostname in hostnames:
        client = create_valkey_client(
            hostname=hostname,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=new_password,
        )
        assert client.ping() is True, (
            f"Failed to authenticate with new admin password on host {hostname}"
        )
        assert client.get(TEST_KEY) == bytes(TEST_VALUE, "utf-8"), (
            f"Failed to read data after admin password update on host {hostname}"
        )

    logger.info("Password update successful after secret was granted")

    # change replication password
    replica_password = "replica-password"
    juju.update_secret(
        identifier=secret_id,
        content={
            CharmUsers.VALKEY_ADMIN.value: new_password,
            CharmUsers.VALKEY_REPLICA.value: replica_password,
        },
    )

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=10),
        timeout=1200,
    )

    # perform pings with the updated replica password
    for hostname in hostnames:
        client = create_valkey_client(
            hostname=hostname,
            username=CharmUsers.VALKEY_REPLICA.value,
            password=replica_password,
        )
        assert client.ping() is True, (
            f"Failed to authenticate with new replica password on host {hostname}"
        )
