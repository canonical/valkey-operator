#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import os
import re
import subprocess
from pathlib import Path

import jubilant

from literals import CharmUsers, Substrate
from statuses import TLSStatuses
from tests.integration.helpers import (
    APP_NAME,
    GLIDE_RUNNER_NAME,
    IMAGE_RESOURCE,
    TLS_CERT_FILE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    does_status_match,
    download_client_certificate_from_unit,
    get_cluster_endpoints,
    get_password,
    set_key,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"
VAULT_NAME = "vault"


def test_build_and_deploy(
    charm: str, juju: jubilant.Juju, substrate: Substrate, glide_runner_charm
) -> None:
    """Deploy the charm under test and a TLS provider."""
    logger.info("Installing vault cli client")
    subprocess.run(
        ["sudo", "snap", "install", "vault"], check=True, text=True, capture_output=True
    )

    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.deploy(glide_runner_charm, app=GLIDE_RUNNER_NAME)
    juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
    juju.deploy(
        "vault-k8s" if substrate == Substrate.K8S else "vault",
        app=VAULT_NAME,
        channel="1.18/edge",
        config={
            "pki_ca_common_name": "mydomain.com",
            "pki_allow_any_name": False,
            "pki_allow_ip_sans": False,
        },
    )
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(
            status,
            APP_NAME,
            GLIDE_RUNNER_NAME,
            idle_period=30,
            unit_count={APP_NAME: NUM_UNITS, GLIDE_RUNNER_NAME: 1},
        ),
        timeout=600,
    )
    juju.wait(lambda status: jubilant.all_blocked(status, VAULT_NAME))


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
    assert unit_name.replace("/", "") in client_cert_sans, "unit name not found in DNS SANs"

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

    logger.info("Remove relation with %s", TLS_NAME)
    juju.remove_relation(f"{APP_NAME}:client-certificates", f"{TLS_NAME}:certificates")
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )


def test_initialize_vault(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Initialize Vault and wait for it to be ready."""
    # follows the procedure for initializing and unsealing Vault as described in
    # https://canonical-vault-charms.readthedocs-hosted.com/en/latest/tutorial/getting_started_k8s/#deploy-vault
    logger.info("Initializing Vault")

    logger.info("Getting the Vault address")
    vault_units = juju.status().get_units(VAULT_NAME)
    vault_unit = next(iter(vault_units.values()))
    vault_ip = (
        juju.status().apps[VAULT_NAME].address
        if substrate == Substrate.K8S
        else vault_unit.public_address
    )
    secrets = juju.secrets()

    logger.info("Extracting Vault's CA certificate")
    vault_ca = None
    for secret in secrets:
        if secret.label == "self-signed-vault-ca-certificate":
            vault_ca = juju.show_secret(identifier=secret.uri, reveal=True).content.get(
                "certificate"
            )
    assert vault_ca, "Vault CA certificate not found in secrets"
    Path("./vault_ca.pem").write_text(vault_ca)

    # point the locally installed Vault client to the Vault deployment
    vault_env = os.environ.copy()
    vault_env["VAULT_CACERT"] = "./vault_ca.pem"
    vault_env["VAULT_ADDR"] = f"https://{vault_ip}:8200"

    # initialize the deployed Vault
    logger.info("Running vault operator init")
    init_cmd = [
        "vault",
        "operator",
        "init",
        "-key-shares=1",
        "-key-threshold=1",
    ]
    init_result = subprocess.run(
        init_cmd, check=True, text=True, capture_output=True, env=vault_env
    )
    logger.info(f"Vault operator init output: {init_result.stdout}")
    init_results_list = [line.strip() for line in init_result.stdout.splitlines() if line.strip()]

    # on init, Vault returns the root token and a key that are required for unsealing Vault
    unseal_key = init_results_list[0].split(":")[1].strip()
    root_token = init_results_list[1].split(":")[1].strip()
    vault_env["VAULT_TOKEN"] = root_token

    # unseal the deployed Vault
    logger.info("Running vault operator unseal")
    unseal_cmd = [
        "vault",
        "operator",
        "unseal",
        unseal_key,
    ]
    unseal_result = subprocess.run(
        unseal_cmd, check=True, text=True, capture_output=True, env=vault_env
    )
    logger.info(f"Vault operator unseal output: {unseal_result.stdout}")

    # authorize Vault charm
    # create a one-time token and store it as a secret
    logger.info("Creating Vault token for the vault charm")
    create_token_cmd = [
        "vault",
        "token",
        "create",
        "-ttl=60m",
    ]
    create_token_result = subprocess.run(
        create_token_cmd, check=True, text=True, capture_output=True, env=vault_env
    )
    logger.info(f"Vault token create output: {create_token_result.stdout}")
    token_regex = r"token\s+([\w\.]+)"

    # extract token using regex
    match = re.search(token_regex, create_token_result.stdout)
    assert match, "Failed to extract token from Vault token create output"
    charm_vault_token = match.group(1)
    secret_id = juju.add_secret(
        "vault-token",
        {
            "token": charm_vault_token,
        },
    )

    assert secret_id, "Failed to create vault-token secret"
    juju.grant_secret("vault-token", VAULT_NAME)

    # authorize the charm to interact with Vault using the token value from the secret
    vault_unit_name = next(iter(vault_units))
    action = juju.run(
        unit=vault_unit_name,
        action="authorize-charm",
        params={
            "secret-id": str(secret_id),
        },
    )

    assert action.status == "completed", "Action should succeed"
    juju.wait(lambda status: are_apps_active_and_agents_idle(status, VAULT_NAME))


def test_certificate_denied(juju: jubilant.Juju) -> None:
    """Process denied certificate request."""
    logger.info("Integrate %s with %s for Intermediate CA", VAULT_NAME, TLS_NAME)
    juju.integrate(f"{VAULT_NAME}:tls-certificates-pki", TLS_NAME)
    juju.wait(lambda status: are_agents_idle(status, VAULT_NAME, idle_period=30), timeout=600)

    logger.info("Integrate Valkey with Vault for client TLS")
    logger.info("Certificate requests should be denied because Vault does not allow IP SANs")
    juju.integrate(f"{APP_NAME}:client-certificates", VAULT_NAME)
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [TLSStatuses.CERTIFICATE_DENIED.value]},
            num_units={APP_NAME: NUM_UNITS},
        ),
        timeout=600,
    )

    logger.info("Ensure access without TLS is still possible")
    endpoints = get_cluster_endpoints(juju, APP_NAME)
    result = set_key(
        juju=juju,
        endpoints=endpoints,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=False,
        key=TEST_KEY,
        value=TEST_VALUE,
    )
    assert result == "OK", "Failed to write data without TLS"

    logger.info("Removing TLS relation again")
    juju.remove_relation(f"{APP_NAME}:client-certificates", VAULT_NAME)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, idle_period=30, unit_count=NUM_UNITS
        ),
        timeout=600,
    )
