#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from time import sleep

import jubilant
import pytest

from literals import (
    INTERNAL_USERS_PASSWORD_CONFIG,
    CharmUsers,
)
from statuses import CharmStatuses, ClusterStatuses
from tests.integration.helpers import (
    APP_NAME,
    INTERNAL_USERS_SECRET_LABEL,
    create_valkey_client,
    does_status_match,
    fast_forward,
    get_cluster_hostnames,
    get_key,
    get_secret_by_label,
    set_key,
    set_password,
)

logger = logging.getLogger(__name__)

# TODO scale up when scaling is implemented
NUM_UNITS = 1
TEST_KEY = "test_key"
TEST_VALUE = "test_value"


def test_build_and_deploy(charm: str, juju: jubilant.Juju) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(charm, num_units=NUM_UNITS, trust=True)
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_app_statuses={APP_NAME: [CharmStatuses.SCALING_NOT_IMPLEMENTED.value]},
        ),
        timeout=600,
        delay=5,
        successes=3,
    )


async def test_authentication(juju: jubilant.Juju) -> None:
    """Assert that we can authenticate to valkey."""
    hostnames = get_cluster_hostnames(juju, APP_NAME)

    # try without authentication
    logger.info("Ensure unauthenticated access fails")
    with pytest.raises(Exception) as exc_info:
        unauth_client = await create_valkey_client(
            hostnames=hostnames, username=None, password=None
        )
        await unauth_client.ping()
    assert "NOAUTH" in str(exc_info.value), "Unauthenticated access did not fail as expected"

    # Authenticate with internal user
    secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    password = secret.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
    assert password is not None, "Admin password secret not found"

    logger.info("Check access works correctly when authenticated")
    client = await create_valkey_client(hostnames=hostnames, password=password)
    auth_result = await client.ping()
    assert auth_result == b"PONG", "Authentication to Valkey cluster failed"


async def test_update_admin_password(juju: jubilant.Juju) -> None:
    """Assert the admin password is updated when adding a user secret to the config."""
    hostnames = get_cluster_hostnames(juju, APP_NAME)

    # create a user secret and grant it to the application
    logger.info("Updating operator password")
    secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    old_password = secret.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
    new_password = "some-password"
    set_password(juju, new_password)

    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: jubilant.all_agents_idle(status, APP_NAME),
        timeout=1200,
        delay=5,
        successes=3,
    )

    logger.info("Ensure password was updated on charm-internal secret")
    updated_secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    assert old_password != updated_secret.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")

    logger.info("Ensure access with old password no longer possible")
    with pytest.raises(Exception) as exc_info:
        unauth_client = await create_valkey_client(
            hostnames=hostnames, username=CharmUsers.VALKEY_ADMIN.value, password=old_password
        )
        await unauth_client.ping()
    assert "WRONGPASS" in str(exc_info.value), "Unauthenticated access did not fail as expected"

    logger.info("Check access with updated password")
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=new_password,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data after admin password update"

    # update the config again and remove the option `admin-password`
    logger.info("Ensure access is still possible after removing config option")
    juju.config(app=APP_NAME, reset=[INTERNAL_USERS_PASSWORD_CONFIG])

    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: jubilant.all_agents_idle(status, APP_NAME),
        timeout=1200,
        delay=5,
        successes=3,
    )

    # make sure we can still read data with the previously set password
    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=new_password,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8")


async def test_update_admin_password_wrong_username(juju: jubilant.Juju) -> None:
    """Assert the admin password is updated when adding a user secret to the config."""
    hostnames = get_cluster_hostnames(juju, APP_NAME)
    secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    old_passwords = {}

    logger.info("Storing old passwords before update")
    for user in CharmUsers:
        if user == CharmUsers.VALKEY_ADMIN:
            continue
        old_passwords[user.value] = secret.get(f"{user.value}-password")

    # create a user secret and grant it to the application
    logger.info("Updating invalid username")
    new_password = "some-password"
    set_password(juju, username="wrong-username", password=new_password)

    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_app_statuses={APP_NAME: [ClusterStatuses.PASSWORD_UPDATE_FAILED.value]},
        ),
        timeout=1200,
        delay=5,
        successes=3,
    )

    logger.info("Updating password correctly now")
    set_password(juju, username=CharmUsers.VALKEY_ADMIN.value, password=new_password)
    # wait for config-changed hook to finish executing
    juju.wait(lambda status: jubilant.all_agents_idle(status, APP_NAME), timeout=1200)

    # perform read operation with the updated password
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=new_password,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data after admin password update"

    logger.info("Comparing other users passwords to previously")
    updated_secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    for user in CharmUsers:
        if user == CharmUsers.VALKEY_ADMIN:
            continue
        assert old_passwords[user.value] == updated_secret.get(f"{user.value}-password"), (
            f"Password for {user} must not be updated"
        )


async def test_user_secret_permissions(juju: jubilant.Juju) -> None:
    """If a user secret is not granted, ensure we can process updated permissions."""
    hostnames = get_cluster_hostnames(juju, APP_NAME)

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
        delay=5,
        successes=3,
    )

    logger.info("Secret access will be granted now - wait for updated password")
    # deferred `config_changed` event will be retried before `update_status`
    with fast_forward(juju):
        juju.grant_secret(identifier=secret_name, app=APP_NAME)
        sleep(20)  # allow some time for the permission to propagate

    # juju.wait(
    #     lambda status: jubilant.all_active(status, APP_NAME),
    #     timeout=1200,
    # )
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_app_statuses={APP_NAME: [CharmStatuses.SCALING_NOT_IMPLEMENTED.value]},
        ),
        timeout=600,
        delay=5,
        successes=3,
    )

    # perform read operation with the updated password
    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=new_password,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data after secret permissions were updated"

    logger.info("Password update successful after secret was granted")


# TODO Once scaling is implemented, add tests to check on password update in non-leader units
