#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import asyncio
import logging

import jubilant
import pytest

from literals import Substrate
from tests.integration.cw_helpers import (
    assert_continuous_writes_increasing,
)
from tests.integration.ha.helpers.helpers import (
    cut_network_from_unit,
    get_sans_from_certificate,
    get_unit_name_from_primary_ip,
    hostname_from_unit,
    is_unit_reachable,
    restore_network_to_unit,
)
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    CharmUsers,
    are_apps_active_and_agents_idle,
    download_client_certificate_from_unit,
    get_cluster_hostnames,
    get_ip_from_unit,
    get_number_connected_replicas,
    get_password,
    get_primary_ip,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
def test_build_and_deploy(
    tls_enabled: bool, charm: str, juju: jubilant.Juju, substrate: Substrate
) -> None:
    """Build the charm-under-test and deploy it with three units."""
    if tls_enabled and substrate == Substrate.K8S:
        pytest.skip("Tests on k8s is the same as no IP will change")

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
@pytest.mark.parametrize("change_ip", [True, False], ids=["change_ip", "no_change_ip"])
async def test_network_cut_primary(  # noqa: C901
    tls_enabled: bool,
    change_ip: bool,
    juju: jubilant.Juju,
    substrate: Substrate,
    chaos_mesh,
    c_writes,
    c_writes_async_clean,
) -> None:
    """Cut the network to the primary unit and verify that a new primary is elected."""
    if tls_enabled:
        if substrate == Substrate.K8S:
            pytest.skip("Tests on k8s is the same as no IP will change")
        download_client_certificate_from_unit(juju, APP_NAME)
    if change_ip and substrate == Substrate.K8S:
        pytest.skip("Changing IP is not applicable for k8s substrate.")

    c_writes.tls_enabled = tls_enabled
    await c_writes.async_clear()
    c_writes.start()

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

    logger.info("Identified container name for primary unit: %s", primary_hostname)
    cut_network_from_unit(substrate, juju.model, machine_name, change_ip=change_ip)

    for unit in juju.status().apps[APP_NAME].units:
        if unit == primary_unit_name:
            continue
        assert not is_unit_reachable(
            juju, hostname_from_unit(juju, unit), primary_hostname, substrate
        ), f"Unit {unit} can still reach the primary unit {primary_hostname} after network cut."

    logger.info(
        "Network successfully cut to primary unit %s at %s. Verifying new primary election...",
        primary_unit_name,
        primary_ip,
    )
    while True:
        try:
            new_primary_ip = get_primary_ip(juju, APP_NAME, tls_enabled=tls_enabled)
            break
        except ValueError as e:
            logger.warning(f"Error getting primary IP after network cut: {e}")
        logger.info("Waiting for new primary to be elected...")
        await asyncio.sleep(10)

    assert new_primary_ip != primary_ip, (
        "Primary IP did not change after cutting network to the primary unit."
    )
    logger.info(
        "New primary IP after network cut: %s vs old primary IP: %s", new_primary_ip, primary_ip
    )

    hostnames = get_cluster_hostnames(juju, APP_NAME)
    # check replica number that it is down to NUM_UNITS - 2
    number_of_replicas = await get_number_connected_replicas(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=tls_enabled,
    )
    assert number_of_replicas == NUM_UNITS - 2, (
        f"Expected {NUM_UNITS - 2} connected replicas, got {number_of_replicas}."
    )
    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=tls_enabled,
    )

    # restore network to the original primary unit
    logger.info("Restoring network to original primary unit at %s", primary_hostname)
    restore_network_to_unit(substrate, juju.model, machine_name)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        )
    )
    c_writes.update()

    for unit in juju.status().apps[APP_NAME].units:
        if unit == primary_unit_name:
            continue
        assert is_unit_reachable(
            juju, hostname_from_unit(juju, unit), primary_hostname, substrate
        ), (
            f"Unit {unit} cannot reach the original primary unit {primary_hostname} after network restoration."
        )

    download_client_certificate_from_unit(juju, APP_NAME, unit_name=primary_unit_name)
    # read ip from cert and check if is a different ip than before if change_ip is True
    certificate_sans = get_sans_from_certificate("./client.pem")
    if change_ip:
        assert primary_ip not in certificate_sans["sans_ip"], (
            "The old IP should not be in SANs of client certificate after network cut and IP change."
        )
        assert get_ip_from_unit(juju, primary_unit_name) in certificate_sans["sans_ip"], (
            "The new IP should be in SANs of client certificate after network cut and IP change."
        )

    hostnames = get_cluster_hostnames(juju, APP_NAME)
    # check replica number that it is back to NUM_UNITS - 1
    number_of_replicas = await get_number_connected_replicas(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=tls_enabled,
    )
    assert number_of_replicas == NUM_UNITS - 1, (
        f"Expected {NUM_UNITS - 1} connected replicas after network restoration, got {number_of_replicas}."
    )

    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=tls_enabled,
    )
