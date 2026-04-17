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
    TLS_CA_FILE,
    TLS_CERT_FILE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
    auth_test,
    does_status_match,
    download_client_certificate_from_unit,
    get_cluster_addresses,
    get_key,
    get_password,
    set_key,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"
CERTIFICATE_EXPIRY_TIME = 360
CA_EXPIRY_TIME = 430


def _prepare_units_for_ca_expiration_test(juju: jubilant.Juju) -> None:
    """Prepare the units for the CA expiration test."""
    for unit_name in juju.status().get_units(APP_NAME):
        logger.info("Updating renewal relative time to 0.6 for unit %s", unit_name)
        search_expression = "\\(refresh_events=\\[self.refresh_tls_certificates_event\\],\\)"
        replace_expression = "\\1renewal_relative_time=0.6,"
        file = f"/var/lib/juju/agents/unit-{unit_name.replace('/', '-')}/charm/src/events/tls.py"
        juju.ssh(
            command=f"sudo sed -i 's|{search_expression}|{replace_expression}|' {file}",
            target=unit_name,
        )


def test_build_and_deploy(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Deploy the charm under test and a TLS provider."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )

    tls_config = {"certificate-validity": "6m", "ca-common-name": "valkey"}
    juju.deploy(TLS_NAME, channel=TLS_CHANNEL, config=tls_config)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )


async def test_certificate_expiration(juju: jubilant.Juju) -> None:
    """Test the TLS certificate expiration and renewal on a running cluster."""
    logger.info("Enabling TLS")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )

    logger.info("Downloading TLS certificate from deployed app.")
    download_client_certificate_from_unit(juju, APP_NAME)

    logger.info("Check access with TLS enabled")
    addresses = get_cluster_addresses(juju, APP_NAME)
    result = await set_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert await get_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with TLS enabled"

    logger.info("Store current certificate before expiration")
    with open(TLS_CERT_FILE, "r") as file:
        old_client_certificate = file.read()
    assert old_client_certificate, "Failed to get current client certificate"

    _prepare_units_for_ca_expiration_test(juju)
    logger.info("Waiting for certificate to expire")
    sleep(CERTIFICATE_EXPIRY_TIME)

    logger.info("Check access with previous certificate fails after expiration")
    with pytest.raises(Exception) as exc_info:
        await auth_test(
            hostnames=addresses,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
            tls_enabled=True,
        )
    assert "Connection error" in str(exc_info.value), (
        "Access with expired certificate did not fail as expected"
    )

    logger.info("Store new certificate after rotation")
    download_client_certificate_from_unit(juju, APP_NAME)
    with open(TLS_CERT_FILE, "r") as file:
        new_client_certificate = file.read()
    assert new_client_certificate, "Failed to get new client certificate"

    logger.info("Ensure certificate has been updated")
    assert new_client_certificate != old_client_certificate, "Client certificate not updated"

    logger.info("Check access with updated certificate")
    download_client_certificate_from_unit(juju, APP_NAME)
    result = await set_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with updated certificate"

    assert await get_key(
        hostnames=addresses,
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


async def test_ca_rotation_by_config_change(juju: jubilant.Juju) -> None:
    """Test the CA rotation.

    The CA certificate should be rotated and the cluster should still be accessible.
    The rotation is triggered by updating the config for `ca-common-name` on the TLS provider side.
    """
    # Rotate the CA certificate
    logger.info("Getting the current CA certificates")
    download_client_certificate_from_unit(juju, APP_NAME)
    with open(TLS_CA_FILE, "r") as ca_file:
        old_ca_certificate = ca_file.read()
    assert old_ca_certificate, "Failed to get current ca certificate"
    with open(TLS_CERT_FILE, "r") as cert_file:
        old_certificate = cert_file.read()
    assert old_certificate, "Failed to get current certificate"

    logger.info("Rotating the CA certificate")
    tls_config = {"certificate-validity": "10d", "ca-common-name": "new-valkey-ca"}
    juju.config(app=TLS_NAME, values=tls_config)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )

    logger.info("Checking if the CA certificates are rotated")
    download_client_certificate_from_unit(juju, APP_NAME)
    with open(TLS_CA_FILE, "r") as ca_file:
        new_ca_certificate = ca_file.read()
    assert new_ca_certificate, "Failed to get updated ca certificate"
    with open(TLS_CERT_FILE, "r") as cert_file:
        new_certificate = cert_file.read()
    assert new_certificate, "Failed to get updated certificate"
    assert old_ca_certificate != new_ca_certificate, "CA certificate was not updated"
    assert old_certificate != new_certificate, "Certificate was not updated"

    logger.info("Check access with updated certificate")
    addresses = get_cluster_addresses(juju, APP_NAME)
    result = await set_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with updated certificate"

    assert await get_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with updated certificate"


async def test_ca_rotation_by_expiration(juju: jubilant.Juju) -> None:
    """Test the CA rotation.

    The CA certificate should be rotated and the cluster should still be accessible.
    The rotation is triggered by the expiration of the CA cert on TLS provider side.
    """
    logger.info("Adjust CA and certificate validity on TLS provider")
    tls_config = {"certificate-validity": "6m", "root-ca-validity": "12m"}
    juju.config(app=TLS_NAME, values=tls_config)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [TLSStatuses.CERTIFICATE_EXPIRING.value]},
            num_units={APP_NAME: NUM_UNITS},
        ),
        timeout=600,
    )

    download_client_certificate_from_unit(juju, APP_NAME)
    with open(TLS_CA_FILE, "r") as ca_file:
        old_ca_certificate = ca_file.read()
    assert old_ca_certificate, "Failed to get current ca certificate"
    with open(TLS_CERT_FILE, "r") as cert_file:
        old_certificate = cert_file.read()
    assert old_certificate, "Failed to get current certificate"

    logger.info("Check access with current TLS certificate")
    addresses = get_cluster_addresses(juju, APP_NAME)
    result = await set_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with TLS enabled"

    assert await get_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with TLS enabled"

    logger.info("Waiting for CA certificate to expire")
    sleep(CA_EXPIRY_TIME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=10, unit_count=NUM_UNITS),
        timeout=600,
    )

    logger.info("Check access with previous certificate fails after expiration")
    with pytest.raises(Exception) as exc_info:
        await auth_test(
            hostnames=addresses,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
            tls_enabled=True,
        )
    assert "Connection error" in str(exc_info.value), (
        "Access with expired certificate did not fail as expected"
    )

    logger.info("Store new certificate after rotation")
    download_client_certificate_from_unit(juju, APP_NAME)
    with open(TLS_CA_FILE, "r") as ca_file:
        new_ca_certificate = ca_file.read()
    assert new_ca_certificate, "Failed to get updated ca certificate"
    with open(TLS_CERT_FILE, "r") as cert_file:
        new_certificate = cert_file.read()
    assert new_certificate, "Failed to get updated certificate"
    assert old_ca_certificate != new_ca_certificate, "CA certificate was not updated"
    assert old_certificate != new_certificate, "Certificate was not updated"

    logger.info("Check access with updated certificate")
    addresses = get_cluster_addresses(juju, APP_NAME)
    result = await set_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with updated certificate"

    assert await get_key(
        hostnames=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
    ) == bytes(TEST_VALUE, "utf-8"), "Failed to read data with updated certificate"
