#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
import pytest

from literals import (
    INTERNAL_USERS_PASSWORD_CONFIG,
    CharmUsers,
    Substrate,
)
from statuses import CharmStatuses, ClusterStatuses
from tests.integration.helpers import (
    APP_NAME,
    GLIDE_RUNNER_NAME,
    IMAGE_RESOURCE,
    INTERNAL_USERS_SECRET_LABEL,
    NoAuthError,
    WrongPassError,
    are_apps_active_and_agents_idle,
    auth_test,
    does_status_match,
    exec_valkey_cli,
    fast_forward,
    get_cluster_addresses,
    get_password,
    get_secret_by_label,
    ping,
    ping_cluster,
    set_key,
    set_password,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"


def test_build_and_deploy(
    charm: str, juju: jubilant.Juju, substrate: Substrate, glide_runner_charm: str
) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.deploy(glide_runner_charm, app=GLIDE_RUNNER_NAME)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, GLIDE_RUNNER_NAME, idle_period=30
        ),
        timeout=600,
        delay=5,
        successes=3,
    )


def test_authentication(juju: jubilant.Juju) -> None:
    """Assert that we can authenticate to valkey."""
    addresses = get_cluster_addresses(juju, APP_NAME)

    # try without authentication
    with pytest.raises(NoAuthError):
        auth_test(juju, cluster_addresses=addresses, username=None, password=None)

    # Authenticate with internal user
    password = get_password(juju, user=CharmUsers.VALKEY_ADMIN)
    assert password is not None, "Admin password secret not found"

    for address in addresses:
        assert (
            "PONG"
            in exec_valkey_cli(address, CharmUsers.VALKEY_ADMIN.value, password, "ping").stdout
        ), "Failed to authenticate with Valkey cluster using CLI"


def test_update_admin_password(juju: jubilant.Juju) -> None:
    """Assert the admin password is updated when adding a user secret to the config."""
    # create a user secret and grant it to the application
    logger.info("Updating operator password")
    old_password = get_password(juju, user=CharmUsers.VALKEY_ADMIN)
    new_password = "some-password"
    set_password(juju, new_password)

    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=1200,
    )

    new_password_secret = get_password(juju, user=CharmUsers.VALKEY_ADMIN)
    assert new_password_secret == new_password, "Admin password not updated in secret"

    addresses = get_cluster_addresses(juju, APP_NAME)
    # confirm old password no longer works
    with pytest.raises(WrongPassError):
        auth_test(
            juju,
            cluster_addresses=addresses,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=old_password,
        )

    assert (
        ping_cluster(juju, APP_NAME, addresses, CharmUsers.VALKEY_ADMIN.value, new_password)
        is True
    ), "Failed to authenticate with new admin password"

    assert (
        set_key(
            juju,
            addresses,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=new_password,
            key=TEST_KEY,
            value=TEST_VALUE,
        )
        == "OK"
    ), "Failed to write data after admin password update"

    # update the config again and remove the option `admin-password`
    logger.info("Ensure access is still possible after removing config option")
    juju.config(app=APP_NAME, reset=[INTERNAL_USERS_PASSWORD_CONFIG])

    # wait for config-changed hook to finish executing
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=1200,
    )

    for address in get_cluster_addresses(juju, APP_NAME):
        assert (
            ping(address, username=CharmUsers.VALKEY_ADMIN.value, password=new_password) is True
        ), (
            f"Failed to authenticate with admin password after removing user secret on host {address}"
        )
        assert (
            exec_valkey_cli(
                address, CharmUsers.VALKEY_ADMIN.value, new_password, f"get {TEST_KEY}"
            ).stdout
            == TEST_VALUE
        ), f"Failed to read data after admin password update on host {address}"


def test_update_admin_password_wrong_username(juju: jubilant.Juju) -> None:
    """Assert the admin password is updated when adding a user secret to the config."""
    # create a user secret and grant it to the application
    secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    old_passwords = {}

    logger.info("Storing old passwords before update")
    for user in CharmUsers:
        if user == CharmUsers.VALKEY_ADMIN:
            continue
        old_passwords[user.value] = secret.get(f"{user.value}-password")
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
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=1200,
    )

    # perform read operation with the updated password
    assert (
        ping_cluster(
            juju=juju,
            app_name=APP_NAME,
            endpoints=get_cluster_addresses(juju, APP_NAME),
            username=CharmUsers.VALKEY_ADMIN.value,
            password=new_password,
        )
        is True
    ), "Failed to authenticate with new admin password"

    assert (
        set_key(
            juju=juju,
            endpoints=get_cluster_addresses(juju, APP_NAME),
            username=CharmUsers.VALKEY_ADMIN.value,
            password=new_password,
            key=TEST_KEY,
            value=TEST_VALUE,
        )
        == "OK"
    ), "Failed to write data after admin password update"

    logger.info("Comparing other users passwords to previously")
    updated_secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    for user in CharmUsers:
        if user == CharmUsers.VALKEY_ADMIN:
            continue
        assert old_passwords[user.value] == updated_secret.get(f"{user.value}-password"), (
            f"Password for {user} must not be updated"
        )


def test_user_secret_permissions(juju: jubilant.Juju) -> None:
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
    juju.grant_secret(identifier=secret_name, app=APP_NAME)
    # deferred `config_changed` event will be retried before `update_status`
    with fast_forward(juju, update_interval="30s"):
        juju.wait(
            lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=10),
            timeout=1200,
        )

    # perform read operation with the updated password
    addresses = get_cluster_addresses(juju, APP_NAME)
    assert ping_cluster(
        juju=juju,
        app_name=APP_NAME,
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=new_password,
    ), "Failed to authenticate with new admin password"

    assert (
        set_key(
            juju=juju,
            endpoints=addresses,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=new_password,
            key=TEST_KEY,
            value=TEST_VALUE,
        )
        == "OK"
    ), "Failed to write data after admin password update"

    for address in addresses:
        assert (
            ping(address, username=CharmUsers.VALKEY_ADMIN.value, password=new_password) is True
        ), (
            f"Failed to authenticate with admin password after removing user secret on host {address}"
        )
        assert (
            exec_valkey_cli(
                address, CharmUsers.VALKEY_ADMIN.value, new_password, f"get {TEST_KEY}"
            ).stdout
            == TEST_VALUE
        ), f"Failed to read data after admin password update on host {address}"

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
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=60),
        timeout=1200,
    )

    # perform pings with the updated replica password
    for address in get_cluster_addresses(juju, APP_NAME):
        assert (
            ping(address, username=CharmUsers.VALKEY_REPLICA.value, password=replica_password)
            is True
        ), (
            f"Failed to authenticate with replica password after removing user secret on host {address}"
        )
