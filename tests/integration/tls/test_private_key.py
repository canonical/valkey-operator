#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
from charmlibs.interfaces.tls_certificates import PrivateKey

from literals import TLS_CLIENT_PRIVATE_KEY_CONFIG, CharmUsers, Substrate
from statuses import TLSStatuses
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CERT_FILE,
    TLS_CHANNEL,
    TLS_KEY_FILE,
    TLS_NAME,
    are_agents_idle,
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


def test_build_and_deploy(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Deploy the charm under test and a TLS provider."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )

    juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )


def test_invalid_private_key(juju: jubilant.Juju) -> None:
    """Ensure an invalid private key is not harmful."""
    logger.info("Adding invalid private key as user secret")
    private_key = "invalid-private-key"
    secret_id = juju.add_secret(
        name=TLS_CLIENT_PRIVATE_KEY_CONFIG,
        content={"private-key": private_key},
    )
    juju.grant_secret(TLS_CLIENT_PRIVATE_KEY_CONFIG, APP_NAME)

    logger.info("Setting configuration option to Valkey")
    juju.config(app=APP_NAME, values={TLS_CLIENT_PRIVATE_KEY_CONFIG: secret_id})
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [TLSStatuses.PRIVATE_KEY_INVALID.value]},
            num_units={APP_NAME: NUM_UNITS},
        ),
        timeout=100,
    )


async def test_valid_private_key(juju: jubilant.Juju) -> None:
    logger.info("Updating user secret with valid private key now")
    private_key = PrivateKey.generate().raw

    juju.update_secret(
        identifier=TLS_CLIENT_PRIVATE_KEY_CONFIG,
        content={"private-key": private_key},
    )
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [TLSStatuses.PRIVATE_KEY_BUT_NO_TLS.value]},
            num_units={APP_NAME: NUM_UNITS},
        ),
        timeout=100,
    )

    logger.info("Enabling client TLS")
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
    with open(TLS_KEY_FILE, "r") as key_file:
        private_key_on_unit = key_file.read()
    assert private_key_on_unit, "Failed to get current client certificate private key"
    assert private_key_on_unit == private_key, "Expected user-provided private key to be used"


async def test_private_key_updated(juju: jubilant.Juju) -> None:
    logger.info("Getting current private key and certificate")
    with open(TLS_KEY_FILE, "r") as key_file:
        current_private_key = key_file.read()
    assert current_private_key, "Failed to get current private key"
    with open(TLS_CERT_FILE, "r") as cert_file:
        current_certificate = cert_file.read()
    assert current_certificate, "Failed to get current certificate"

    logger.info("Updating the private key")
    new_private_key = PrivateKey.generate().raw
    juju.update_secret(
        identifier=TLS_CLIENT_PRIVATE_KEY_CONFIG,
        content={"private-key": new_private_key},
    )
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

    logger.info("Getting and comparing updated private key and certificate")
    with open(TLS_KEY_FILE, "r") as key_file:
        updated_private_key = key_file.read()
    assert updated_private_key, "Failed to get updated private key"
    assert updated_private_key != current_private_key, "Private key was not updated"
    assert updated_private_key == new_private_key, "Private key does not match after update"

    with open(TLS_CERT_FILE, "r") as cert_file:
        updated_certificate = cert_file.read()
    assert updated_certificate, "Failed to get updated certificate"
    assert updated_certificate != current_certificate, "Certificate was not updated"
