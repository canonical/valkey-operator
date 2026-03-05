#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from time import sleep

import jubilant
import pytest

from literals import CharmUsers, Substrate
from statuses import TLSStatuses
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    auth_test,
    does_status_match,
    download_client_certificate_from_unit,
    get_cluster_hostnames,
    get_key,
    get_password,
    set_key,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"
CERTIFICATE_EXPIRY_TIME = 320


def test_build_and_deploy(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Deploy the charm under test and a TLS provider."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
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
    logger.info("Check access with TLS enabled")
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with TLS enabled"

    logger.info("Check access without certs fails when TLS enabled")
    with pytest.raises(Exception) as exc_info:
        await auth_test(hostnames, username=None, password=None)
    assert "Connection error" in str(exc_info.value), "Access without TLS did not fail as expected"


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


async def test_disable_tls(juju: jubilant.Juju) -> None:
    """Disable TLS on a running cluster and check if it is still accessible."""
    logger.info("Removing client-certificates relation")
    juju.remove_relation(f"{APP_NAME}:client-certificates", f"{TLS_NAME}:certificates")

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS + 1),
        timeout=600,
    )

    hostnames = get_cluster_hostnames(juju, APP_NAME)
    logger.info("Check access with TLS disabled")
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=False,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data after TLS was disabled"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=False,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data after TLS was disabled"


async def test_enable_tls(juju: jubilant.Juju) -> None:
    """Enable TLS on a running cluster and check if it is still accessible."""
    logger.info("Enabling client TLS")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS + 1),
        timeout=600,
    )

    logger.info("Downloading TLS certificates from deployed app.")
    download_client_certificate_from_unit(juju, APP_NAME)

    hostnames = get_cluster_hostnames(juju, APP_NAME)
    logger.info("Check access with TLS enabled")
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with TLS enabled"

    logger.info("Check access without certs fails when TLS enabled")
    with pytest.raises(Exception) as exc_info:
        await auth_test(hostnames, username=None, password=None)
    assert "Connection error" in str(exc_info.value), "Access without TLS did not fail as expected"


async def test_certificate_expiration(juju: jubilant.Juju) -> None:
    """Test the TLS certificate expiration and renewal on a running cluster."""
    logger.info("Disable TLS")
    juju.remove_relation(f"{APP_NAME}:client-certificates", f"{TLS_NAME}:certificates")

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS + 1),
        timeout=600,
    )

    logger.info("Check access with TLS disabled")
    hostnames = get_cluster_hostnames(juju, APP_NAME)
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=False,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data after TLS was disabled"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=False,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data after TLS was disabled"

    logger.info(f"Configure {TLS_NAME} to issue certificates with 5m validity")
    tls_config = {"certificate-validity": "5m"}
    juju.config(app=TLS_NAME, values=tls_config)

    logger.info("Enable TLS again")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS + 1),
        timeout=600,
    )

    logger.info("Downloading TLS certificate from deployed app.")
    download_client_certificate_from_unit(juju, APP_NAME)

    logger.info("Check access with TLS enabled")
    hostnames = get_cluster_hostnames(juju, APP_NAME)
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with TLS enabled"

    logger.info("Store current certificate before expiration")
    with open("client.pem", "r") as file:
        old_client_certificate = file.read()
    assert old_client_certificate, "Failed to get current client certificate"

    logger.info("Waiting for certificates to expire")
    sleep(CERTIFICATE_EXPIRY_TIME)

    logger.info("Check access with previous certificate fails after expiration")
    with pytest.raises(Exception) as exc_info:
        await auth_test(
            hostnames=hostnames,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
            tls_enabled=True,
        )
    assert "Connection error" in str(exc_info.value), (
        "Access with expired certificate did not fail as expected"
    )

    logger.info("Store new certificate after rotation")
    download_client_certificate_from_unit(juju, APP_NAME)
    with open("client.pem", "r") as file:
        new_client_certificate = file.read()
    assert new_client_certificate, "Failed to get new client certificate"

    logger.info("Ensure certificate has been updated")
    assert new_client_certificate != old_client_certificate, "Client certificate not updated"

    logger.info("Check access with updated certificate")
    download_client_certificate_from_unit(juju, APP_NAME)
    result = await set_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with updated certificate"

    assert await get_key(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with updated certificate"

    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [TLSStatuses.CERTIFICATE_EXPIRING.value]},
            num_units={APP_NAME: NUM_UNITS},
        ),
        timeout=100,
    )
