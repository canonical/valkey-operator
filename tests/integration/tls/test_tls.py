#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant

from literals import CharmUsers, Substrate
from tests.integration.helpers import (
    APP_NAME,
    GLIDE_RUNNER_NAME,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    auth_test,
    download_client_certificate_from_unit,
    get_cluster_endpoints,
    get_key,
    get_password,
    set_key,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"


def test_build_and_deploy(
    charm: str, juju: jubilant.Juju, substrate: Substrate, glide_runner_charm: str
) -> None:
    """Deploy the charm under test and a TLS provider."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.deploy(glide_runner_charm, app=GLIDE_RUNNER_NAME)
    juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(
            status,
            APP_NAME,
            GLIDE_RUNNER_NAME,
            idle_period=30,
            unit_count={
                APP_NAME: NUM_UNITS,
                GLIDE_RUNNER_NAME: 1,
            },
        ),
        timeout=900,
    )


def test_tls_enabled(juju: jubilant.Juju) -> None:
    """Check if the TLS has been enabled on app startup."""
    logger.info("Downloading TLS certificates from deployed app.")
    download_client_certificate_from_unit(juju, APP_NAME)

    endpoints = get_cluster_endpoints(juju, APP_NAME)
    logger.info("Check access with TLS enabled")
    result = set_key(
        juju=juju,
        endpoints=endpoints,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert (
        get_key(
            juju=juju,
            endpoints=endpoints,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
            tls_enabled=True,
            key=TEST_KEY,
        )
        == TEST_VALUE
    ), "Failed to read data with TLS enabled"

    logger.info("Check access without certs fails when TLS enabled")

    assert not auth_test(juju, endpoints, username=None, password=None)


def test_scale_up_with_tls_enabled(juju: jubilant.Juju) -> None:
    """Ensure a new unit can be added to a cluster with client TLS enabled."""
    logger.info("Add a unit to the cluster")
    juju.add_unit(APP_NAME, num_units=1)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, idle_period=30, unit_count=NUM_UNITS + 1
        ),
        timeout=1200,
    )


def test_disable_tls(juju: jubilant.Juju) -> None:
    """Disable TLS on a running cluster and check if it is still accessible."""
    logger.info("Removing client-certificates relation")
    juju.remove_relation(f"{APP_NAME}:client-certificates", f"{TLS_NAME}:certificates")

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS + 1),
        timeout=600,
    )

    endpoints = get_cluster_endpoints(juju, APP_NAME)
    logger.info("Check access with TLS disabled")
    result = set_key(
        juju=juju,
        endpoints=endpoints,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=False,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data after TLS was disabled"

    assert (
        get_key(
            juju=juju,
            endpoints=endpoints,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
            tls_enabled=False,
            key=TEST_KEY,
        )
        == TEST_VALUE
    ), "Failed to read data after TLS was disabled"


def test_enable_tls(juju: jubilant.Juju) -> None:
    """Enable TLS on a running cluster and check if it is still accessible."""
    logger.info("Enabling client TLS")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS + 1),
        timeout=600,
    )

    logger.info("Downloading TLS certificates from deployed app.")
    download_client_certificate_from_unit(juju, APP_NAME)

    endpoints = get_cluster_endpoints(juju, APP_NAME)
    logger.info("Check access with TLS enabled")
    result = set_key(
        juju=juju,
        endpoints=endpoints,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert (
        get_key(
            juju=juju,
            endpoints=endpoints,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
            tls_enabled=True,
            key=TEST_KEY,
        )
        == TEST_VALUE
    ), "Failed to read data with TLS enabled"

    logger.info("Check access without certs fails when TLS enabled")
    assert not auth_test(juju, endpoints, username=None, password=None)
