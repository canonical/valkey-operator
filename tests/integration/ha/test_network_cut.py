#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
import pytest
from tenacity import Retrying, stop_after_attempt, wait_fixed

from literals import Substrate
from tests.integration.cw_helpers import (
    assert_continuous_writes_increasing,
    configure_cw_runner,
    start_continuous_writes,
    stop_continuous_writes,
)
from tests.integration.ha.helpers.helpers import (
    cut_network_from_unit,
    get_sans_from_certificate,
    get_unit_name_from_primary_ip,
    hostname_from_unit,
    is_endpoint_in_sentinels,
    is_unit_reachable,
    lxd_get_controller_hostname,
    restore_network_to_unit,
    wait_network_restore,
)
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    are_apps_active_and_agents_idle,
    download_client_certificate_from_unit,
    get_cluster_addresses,
    get_ip_from_unit,
    get_number_connected_replicas,
    get_primary_ip,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
def test_build_and_deploy(
    tls_enabled: bool, charm: str, juju: jubilant.Juju, substrate: Substrate
) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )

    if tls_enabled:
        juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
        juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    assert len(juju.status().apps[APP_NAME].units) == NUM_UNITS, (
        f"Unexpected number of units after initial deploy: expected {NUM_UNITS}, got {len(juju.status().apps[APP_NAME].units)}"
    )


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
@pytest.mark.parametrize("ip_change", [True, False], ids=["ip_change", "no_ip_change"])
async def test_network_cut_primary(  # noqa: C901
    tls_enabled: bool,
    ip_change: bool,
    juju: jubilant.Juju,
    substrate: Substrate,
    chaos_mesh,
    glide_runner,
) -> None:
    """Cut the network to the primary unit and verify that a new primary is elected."""
    if ip_change and substrate == Substrate.K8S:
        pytest.skip("Changing IP is not applicable for k8s substrate.")

    download_client_certificate_from_unit(juju, APP_NAME)
    addresses = get_cluster_addresses(juju, APP_NAME)

    configure_cw_runner(juju, valkey_app=APP_NAME, tls_enabled=tls_enabled, substrate=substrate)
    start_continuous_writes(juju, clear=True)

    # Get the current primary unit
    primary_ip = get_primary_ip(juju, APP_NAME, tls_enabled=tls_enabled)
    assert primary_ip, "Failed to get primary endpoint from Juju status."

    # Cut the network to the primary unit
    logger.info("Cutting network to primary unit at %s", primary_ip)
    primary_unit_name = get_unit_name_from_primary_ip(juju, primary_ip, substrate)

    download_client_certificate_from_unit(juju, APP_NAME, unit_name=primary_unit_name)

    primary_hostname = hostname_from_unit(juju, primary_unit_name)
    machine_name = primary_hostname
    if substrate == Substrate.K8S:
        primary_hostname = f"{primary_hostname}.{APP_NAME}-endpoints"

    primary_endpoint = primary_ip if substrate == Substrate.VM else primary_hostname

    logger.info("Identified container name for primary unit: %s", primary_hostname)
    cut_network_from_unit(substrate, juju.model, machine_name, ip_change=ip_change)

    # on K8s the controller is on a different namespace
    if substrate == Substrate.VM:
        controller_hostname = lxd_get_controller_hostname(juju)
        assert not is_unit_reachable(
            juju,
            from_host=controller_hostname,
            to_host=primary_hostname,
            substrate=substrate,
            number_of_retries=3,
        ), (
            f"Controller {controller_hostname} can still reach the primary unit {primary_hostname} after network cut."
        )

    for unit in juju.status().apps[APP_NAME].units:
        if unit == primary_unit_name:
            continue
        assert not is_unit_reachable(
            juju,
            hostname_from_unit(juju, unit),
            primary_hostname,
            substrate,
            number_of_retries=3,
        ), f"Unit {unit} can still reach the primary unit {primary_hostname} after network cut."

    logger.info("Verifying new primary election...")

    new_primary_ip = None
    # failover should happen after 30s
    for attempt in Retrying(stop=stop_after_attempt(4), wait=wait_fixed(10), reraise=True):
        with attempt:
            try:
                # we exclude the old primary ip because on k8s the unit is reachable by ip
                # from outside k8s and is forming its own cluster
                new_primary_ip = get_primary_ip(
                    juju,
                    APP_NAME,
                    tls_enabled=tls_enabled,
                    addresses=[address for address in addresses if address != primary_ip],
                )
                break
            except ValueError as e:
                logger.warning(f"Error getting primary IP after network cut: {e}")
                logger.info("Waiting for new primary to be elected...")
                raise

    assert new_primary_ip and new_primary_ip != primary_ip, (
        f"Primary IP did not change after cutting network to the primary unit. {new_primary_ip} vs old primary IP: {primary_ip}"
    )
    logger.info(
        "New primary IP after network cut: %s vs old primary IP: %s",
        new_primary_ip,
        primary_ip,
    )

    logger.info("Checking number of connected replicas after network cut...")
    # check replica number that it is down to NUM_UNITS - 2
    # retry in case cluster hasn't stabilized yet after primary cut and new primary election
    for attempt in Retrying(stop=stop_after_attempt(10), wait=wait_fixed(10), reraise=True):
        with attempt:
            number_of_replicas = get_number_connected_replicas(juju)
            assert number_of_replicas == NUM_UNITS - 2, (
                f"Expected {NUM_UNITS - 2} connected replicas, got {number_of_replicas}."
            )

    logger.info(
        "Verifying that new primary endpoint is marked as down in sentinels list of other replicas..."
    )
    for address in addresses:
        if address == primary_ip:
            continue
        assert is_endpoint_in_sentinels(
            juju,
            endpoint=primary_endpoint,
            hostname=address,
            status="s_down",
            tls_enabled=tls_enabled,
        ), (
            f"The old primary endpoint should be marked as down in sentinels list of hostname {address} after network cut."
        )

    assert_continuous_writes_increasing(juju)

    # restore network to the original primary unit
    logger.info("Restoring network to original primary unit at %s", primary_hostname)
    restore_network_to_unit(substrate, juju.model, machine_name, ip_change=ip_change)
    wait_network_restore(
        juju=juju,
        substrate=substrate,
        model_name=juju.model,
        app_name=APP_NAME,
        hostname=primary_hostname,
        old_ip=primary_ip,
        ip_change=ip_change,
        unit_count=NUM_UNITS,
    )
    configure_cw_runner(
        juju, valkey_app=APP_NAME, tls_enabled=tls_enabled, substrate=substrate
    )  # update hostnames after network restore

    logger.info(
        "Verifying that all units can reach the original primary unit at %s...",
        primary_hostname,
    )
    for unit in juju.status().apps[APP_NAME].units:
        if unit == primary_unit_name:
            continue
        assert is_unit_reachable(
            juju,
            from_host=hostname_from_unit(juju, unit),
            to_host=primary_hostname,
            substrate=substrate,
        ), (
            f"Unit {unit} cannot reach the original primary unit {primary_hostname} after network restoration."
        )

    download_client_certificate_from_unit(juju, APP_NAME, unit_name=primary_unit_name)
    new_unit_ip = get_ip_from_unit(juju, primary_unit_name)

    # we do not use IPs in certificates for k8s, so no need to check SANs for IP changes
    if substrate == Substrate.VM:
        # read ip from cert and check if is a different ip than before if ip_change is True
        # tolerate delays in certificate update by retrying for up to 100 seconds with 10 second intervals
        for attempt in Retrying(stop=stop_after_attempt(10), wait=wait_fixed(10), reraise=True):
            with attempt:
                download_client_certificate_from_unit(juju, APP_NAME, unit_name=primary_unit_name)
                certificate_sans = get_sans_from_certificate("./client.pem")
                if ip_change:
                    assert primary_ip not in certificate_sans["sans_ip"], (
                        "The old IP should not be in SANs of client certificate after network cut and IP change."
                    )
                    assert new_unit_ip in certificate_sans["sans_ip"], (
                        "The new IP should be in SANs of client certificate after network cut and IP change."
                    )

    addresses = get_cluster_addresses(juju, APP_NAME)
    # check replica number that it is back to NUM_UNITS - 1
    # sometimes it takes some time for the old primary to be marked as replica and for sentinels to update their status, so we add a retry here
    for attempt in Retrying(stop=stop_after_attempt(10), wait=wait_fixed(10), reraise=True):
        with attempt:
            number_of_replicas = get_number_connected_replicas(juju)
            assert number_of_replicas == NUM_UNITS - 1, (
                f"Expected {NUM_UNITS - 1} connected replicas after network restoration, got {number_of_replicas}."
            )

    logger.info("Verifying endpoint presence in sentinels")

    for address in addresses:
        if address == new_unit_ip:
            continue
        if ip_change:
            assert not is_endpoint_in_sentinels(
                juju, primary_endpoint, address, tls_enabled=tls_enabled
            ), (
                f"The old primary endpoint should not be present in sentinels list of hostname {address} after network cut and IP change."
            )
            assert is_endpoint_in_sentinels(juju, new_unit_ip, address, tls_enabled=tls_enabled), (
                f"The new primary IP should be present in sentinels list of hostname {address} after network cut and IP change."
            )
        else:
            assert is_endpoint_in_sentinels(
                juju, primary_endpoint, address, tls_enabled=tls_enabled
            ), (
                f"The old primary endpoint should be present in sentinels list of hostname {address} after network cut and no IP change."
            )

    assert_continuous_writes_increasing(juju)
    stop_continuous_writes(juju)
