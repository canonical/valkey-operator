#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import subprocess

import jubilant

from literals import Substrate
from statuses import TLSStatuses
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CERT_FILE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    does_status_match,
    download_client_certificate_from_unit,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3


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


def test_extra_sans_config_option(juju: jubilant.Juju) -> None:
    """Configure extra sans for the TLS certificates."""
    logger.info("Set config to invalid sans value")
    config_value = "-my.hostname"
    juju.config(app=APP_NAME, values={"certificate-extra-sans": config_value})

    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [TLSStatuses.SANS_CONFIG_INVALID.value]},
            num_units={APP_NAME: NUM_UNITS},
        ),
        timeout=100,
    )

    download_client_certificate_from_unit(juju, APP_NAME)
    client_cert_sans = subprocess.getoutput(
        f"openssl x509 -noout -ext subjectAltName -in {TLS_CERT_FILE}"
    )
    assert config_value not in client_cert_sans, (
        f"config value {config_value} found in certificate sans {client_cert_sans}"
    )

    logger.info("Configure valid extra-sans")
    config_value = "server-{unit}.valkey"
    juju.config(app=APP_NAME, values={"certificate-extra-sans": config_value})

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, unit_count=NUM_UNITS),
        timeout=100,
    )

    # this will download the client cert from application.units[0]
    download_client_certificate_from_unit(juju, APP_NAME)
    client_cert_sans = subprocess.getoutput(
        f"openssl x509 -noout -ext subjectAltName -in {TLS_CERT_FILE}"
    )
    unit_name = next(iter(juju.status().get_units(APP_NAME)))
    expected_sans = config_value.replace("{unit}", unit_name.split("/")[-1])
    assert expected_sans in client_cert_sans, (
        f"expected sans {expected_sans} not found in certificate sans {client_cert_sans}"
    )

    logger.info("Resetting configuration for extra-sans")
    juju.config(app=APP_NAME, reset="certificate-extra-sans")

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, unit_count=NUM_UNITS)
    )

    download_client_certificate_from_unit(juju, APP_NAME)
    client_cert_sans = subprocess.getoutput(
        f"openssl x509 -noout -ext subjectAltName -in {TLS_CERT_FILE}"
    )
    assert expected_sans not in client_cert_sans, (
        f"sans value {expected_sans} found in certificate sans {client_cert_sans}"
    )
