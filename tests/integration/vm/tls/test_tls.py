#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
import pytest

from literals import CharmUsers
from tests.integration.helpers import (
    APP_NAME,
    INTERNAL_USERS_SECRET_LABEL,
    TLS_NAME,
    are_agents_idle,
    auth_test,
    download_client_certificate_from_unit,
    get_cluster_hostnames,
    get_key,
    get_secret_by_label,
    set_key,
)

logger = logging.getLogger(__name__)

# TODO scale up when scaling is implemented
NUM_UNITS = 1
TEST_KEY = "test_key"
TEST_VALUE = "test_value"


def test_build_and_deploy(charm: str, juju: jubilant.Juju) -> None:
    """Deploy the charm under test and a TLS provider."""
    juju.deploy(charm, num_units=NUM_UNITS, trust=True)
    juju.deploy(TLS_NAME, channel="1/edge")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )


async def test_tls_enabled(juju: jubilant.Juju) -> None:
    """Check if the TLS has been enabled on app startup."""
    logger.info("Downloading TLS certificates from deployed app.")
    download_client_certificate_from_unit(juju, APP_NAME)

    hostnames = get_cluster_hostnames(juju, APP_NAME)
    secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    password = secret.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
    assert password is not None, "Admin password secret not found"

    logger.info("Check access with TLS enabled")
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=password,
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=password,
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with TLS enabled"

    logger.info("Check access without certs fails when TLS enabled")
    with pytest.raises(Exception) as exc_info:
        await auth_test(hostnames, username=None, password=None)
    assert "Connection error" in str(exc_info.value), "Access without TLS did not fail as expected"


async def test_disable_tls(juju: jubilant.Juju) -> None:
    """Disable TLS on a running cluster and check if it is still accessible."""
    logger.info("Removing client-certificates relation")
    juju.remove_relation(f"{APP_NAME}:client-certificates", f"{TLS_NAME}:certificates")

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )

    hostnames = get_cluster_hostnames(juju, APP_NAME)
    secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    password = secret.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
    assert password is not None, "Admin password secret not found"

    logger.info("Check access with TLS disabled")
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=password,
        tls_enabled=False,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data after TLS was disabled"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=password,
        tls_enabled=False,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data after TLS was disabled"


async def test_enable_tls(juju: jubilant.Juju) -> None:
    """Enable TLS on a running cluster and check if it is still accessible."""
    logger.info("Enabling client TLS")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )

    logger.info("Downloading TLS certificates from deployed app.")
    download_client_certificate_from_unit(juju, APP_NAME)

    hostnames = get_cluster_hostnames(juju, APP_NAME)
    secret = get_secret_by_label(juju, label=INTERNAL_USERS_SECRET_LABEL)
    password = secret.get(f"{CharmUsers.VALKEY_ADMIN.value}-password")
    assert password is not None, "Admin password secret not found"

    logger.info("Check access with TLS enabled")
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=password,
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=password,
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with TLS enabled"

    logger.info("Check access without certs fails when TLS enabled")
    with pytest.raises(Exception) as exc_info:
        await auth_test(hostnames, username=None, password=None)
    assert "Connection error" in str(exc_info.value), "Access without TLS did not fail as expected"
