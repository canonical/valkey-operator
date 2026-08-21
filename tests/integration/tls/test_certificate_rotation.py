#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from time import monotonic, sleep
from typing import NamedTuple

import jubilant
import pytest
from tenacity import Retrying, stop_after_delay, wait_fixed

from literals import CharmUsers, Substrate
from statuses import TLSStatuses
from tests.integration.helpers import (
    APP_NAME,
    DEPLOY_TIMEOUT_S,
    DEPLOY_TIMEOUT_TLS_S,
    GLIDE_RUNNER_NAME,
    IMAGE_RESOURCE,
    TLS_CA_FILE,
    TLS_CERT_FILE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
    auth_test,
    does_status_match,
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
CERTIFICATE_EXPIRY_TIME = 600
# The provider renews a CA `certificate-validity` before it expires, i.e. 15 min after the
# `rotated_ca` fixture configures a 25 min CA with 10 min certificates, and the units pick the new
# CA up at their next certificate renewal (every 0.6 x 10 min = 6 min): +18 min after that config,
# safely after the fixture finished (its wait is bounded to +15 min) and 3 min clear of the
# neighbouring renewals either side of the CA renewal.
CA_EXPIRY_TIME = 1080


class CARotation(NamedTuple):
    """The CA rotation performed by the `rotated_ca` fixture, and the material either side of it."""

    config_time: float
    """`monotonic()` when the provider was reconfigured, i.e. when the CA clocks started."""
    old_ca: str
    old_certificate: str
    new_ca: str
    new_certificate: str


@pytest.fixture(scope="module")
def rotated_ca(juju: jubilant.Juju) -> CARotation:
    """Rotate the CA on the provider, capturing the material either side of the rotation.

    Module-scoped, so both CA rotation tests share one rotation: the short CA validity configured
    here also schedules the provider-side CA renewal that `test_ca_rotation_by_expiration` waits
    for, so that test does not have to rotate the CA a second time first - which would add ~15 min
    to a job that already runs close to the 75 min CI limit.
    """
    logger.info("Getting the current CA certificates")
    download_client_certificate_from_unit(juju, APP_NAME)
    with open(TLS_CA_FILE, "r") as ca_file:
        old_ca = ca_file.read()
    with open(TLS_CERT_FILE, "r") as cert_file:
        old_certificate = cert_file.read()

    logger.info("Rotating the CA certificate")
    tls_config = {
        "certificate-validity": "10m",
        "root-ca-validity": "25m",
        "ca-common-name": "new-valkey-ca",
    }
    config_time = monotonic()
    juju.config(app=TLS_NAME, values=tls_config)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=DEPLOY_TIMEOUT_TLS_S,
    )

    logger.info("Getting the rotated CA certificates")
    download_client_certificate_from_unit(juju, APP_NAME)
    with open(TLS_CA_FILE, "r") as ca_file:
        new_ca = ca_file.read()
    with open(TLS_CERT_FILE, "r") as cert_file:
        new_certificate = cert_file.read()

    return CARotation(config_time, old_ca, old_certificate, new_ca, new_certificate)


def _prepare_units_for_ca_expiration_test(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Prepare the units for the CA expiration test."""
    for unit_name in juju.status().get_units(APP_NAME):
        logger.info("Updating renewal relative time to 0.6 for unit %s", unit_name)
        search_expression = "\\(refresh_events=\\[self.refresh_tls_certificates_event\\],\\)"
        replace_expression = "\\1renewal_relative_time=0.6,"
        file = f"/var/lib/juju/agents/unit-{unit_name.replace('/', '-')}/charm/src/events/tls.py"
        sudo = "sudo " if substrate == Substrate.VM else ""
        juju.ssh(
            command=f"{sudo}sed -i 's|{search_expression}|{replace_expression}|' {file}",
            target=unit_name,
        )


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

    tls_config = {"certificate-validity": "10m", "ca-common-name": "valkey"}
    juju.deploy(TLS_NAME, channel=TLS_CHANNEL, config=tls_config)
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
        timeout=DEPLOY_TIMEOUT_S,
    )


def test_certificate_expiration(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Test the TLS certificate expiration and renewal on a running cluster."""
    _prepare_units_for_ca_expiration_test(juju, substrate)

    logger.info("Enabling TLS")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    # The renewal_relative_time=0.6 patch above makes the first renewal (and its rolling
    # sentinel restart) land ~6 min in, inside this wait — it needs the TLS budget.
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=DEPLOY_TIMEOUT_TLS_S,
    )

    logger.info("Downloading TLS certificate from deployed app.")
    download_client_certificate_from_unit(juju, APP_NAME)

    logger.info("Check access with TLS enabled")
    endpoints = get_cluster_endpoints(juju, APP_NAME)
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

    logger.info("Store current certificate before expiration")
    with open(TLS_CERT_FILE, "r") as file:
        old_client_certificate = file.read()
    assert old_client_certificate, "Failed to get current client certificate"

    logger.info("Waiting for certificate to expire")
    # Part of the validity already elapsed while TLS was being enabled: poll for the expiry
    # instead of sleeping through the whole of it.
    for attempt in Retrying(
        stop=stop_after_delay(CERTIFICATE_EXPIRY_TIME), wait=wait_fixed(15), reraise=True
    ):
        with attempt:
            logger.info("Check access with previous certificate fails after expiration")
            assert not auth_test(
                juju=juju,
                endpoints=endpoints,
                username=CharmUsers.VALKEY_ADMIN.value,
                password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
                tls_enabled=True,
            ), "Expired certificate is still accepted"

    logger.info("Store new certificate after rotation")
    download_client_certificate_from_unit(juju, APP_NAME)
    with open(TLS_CERT_FILE, "r") as file:
        new_client_certificate = file.read()
    assert new_client_certificate, "Failed to get new client certificate"

    logger.info("Ensure certificate has been updated")
    assert new_client_certificate != old_client_certificate, "Client certificate not updated"

    logger.info("Check access with updated certificate")
    download_client_certificate_from_unit(juju, APP_NAME)
    result = set_key(
        juju=juju,
        endpoints=endpoints,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with updated certificate"

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
    ), "Failed to read data with updated certificate"

    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [TLSStatuses.CERTIFICATE_EXPIRING.value]},
            num_units={APP_NAME: NUM_UNITS},
        ),
        timeout=100,
    )


def test_ca_rotation_by_config_change(juju: jubilant.Juju, rotated_ca: CARotation) -> None:
    """Test the CA rotation.

    The CA certificate should be rotated and the cluster should still be accessible.
    The rotation is triggered by updating the config for `ca-common-name` on the TLS provider side.
    """
    assert rotated_ca.old_ca, "Failed to get current ca certificate"
    assert rotated_ca.old_certificate, "Failed to get current certificate"

    logger.info("Checking if the CA certificates are rotated")
    assert rotated_ca.new_ca, "Failed to get updated ca certificate"
    assert rotated_ca.new_certificate, "Failed to get updated certificate"
    assert rotated_ca.old_ca != rotated_ca.new_ca, "CA certificate was not updated"
    assert rotated_ca.old_certificate != rotated_ca.new_certificate, "Certificate was not updated"

    logger.info("Check access with updated certificate")
    endpoints = get_cluster_endpoints(juju, APP_NAME)
    result = set_key(
        juju=juju,
        endpoints=endpoints,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=True,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data with updated certificate"

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
    ), "Failed to read data with updated certificate"


def test_ca_rotation_by_expiration(juju: jubilant.Juju, rotated_ca: CARotation) -> None:
    """Test the CA rotation.

    The CA certificate should be rotated and the cluster should still be accessible.
    The rotation is triggered by the expiration of the CA cert on TLS provider side.
    """
    # The material the `rotated_ca` fixture downloaded after its rotation is still the current one
    # and predates the provider-side CA renewal its short CA validity scheduled.
    old_ca_certificate = rotated_ca.new_ca
    assert old_ca_certificate, "Failed to get current ca certificate"
    old_certificate = rotated_ca.new_certificate
    assert old_certificate, "Failed to get current certificate"

    logger.info("Check access with current TLS certificate")
    endpoints = get_cluster_endpoints(juju, APP_NAME)
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

    logger.info("Waiting for CA certificate to expire")
    sleep(max(0.0, rotated_ca.config_time + CA_EXPIRY_TIME - monotonic()))
    # The units pick the renewed CA up at their next certificate renewal, then rotate: poll for
    # the old material being rejected rather than guessing how long that takes.
    for attempt in Retrying(
        stop=stop_after_delay(CERTIFICATE_EXPIRY_TIME), wait=wait_fixed(15), reraise=True
    ):
        with attempt:
            logger.info("Check access with previous certificate fails after expiration")
            assert not auth_test(
                juju=juju,
                endpoints=endpoints,
                username=CharmUsers.VALKEY_ADMIN.value,
                password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
                tls_enabled=True,
            ), "Client certificate of the previous CA is still accepted"
    # The rotation is still rolling through the units at this point: keep re-downloading the
    # material until it is the new CA's and the cluster accepts it.
    for attempt in Retrying(
        stop=stop_after_delay(DEPLOY_TIMEOUT_TLS_S), wait=wait_fixed(15), reraise=True
    ):
        with attempt:
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
            result = set_key(
                juju=juju,
                endpoints=endpoints,
                username=CharmUsers.VALKEY_ADMIN.value,
                password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
                tls_enabled=True,
                key=TEST_KEY,
                value=TEST_VALUE,
            )
            assert result == "OK", "Failed to write data with updated certificate"

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
            ), "Failed to read data with updated certificate"

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=10, unit_count=NUM_UNITS),
        timeout=DEPLOY_TIMEOUT_TLS_S,
    )
