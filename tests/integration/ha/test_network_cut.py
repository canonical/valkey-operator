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
)
from tests.integration.ha.helpers.helpers import (
    cut_network_from_unit,
    endpoint_in_sentinels,
    get_sans_from_certificate,
    get_unit_name_from_primary_ip,
    hostname_from_unit,
    is_unit_reachable,
    lxd_get_controller_hostname,
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
    if change_ip and substrate == Substrate.K8S:
        pytest.skip("Changing IP is not applicable for k8s substrate.")

    download_client_certificate_from_unit(juju, APP_NAME)
    hostnames = get_cluster_hostnames(juju, APP_NAME)

    c_writes.tls_enabled = tls_enabled
    await c_writes.async_clear()
    c_writes.start()

    # Get the current primary unit
    old_primary_endpoint = get_primary_ip(juju, APP_NAME, tls_enabled=tls_enabled)
    assert old_primary_endpoint, "Failed to get primary endpoint from Juju status."

    # Cut the network to the primary unit
    logger.info("Cutting network to primary unit at %s", old_primary_endpoint)
    primary_unit_name = get_unit_name_from_primary_ip(juju, old_primary_endpoint, substrate)

    download_client_certificate_from_unit(juju, APP_NAME, unit_name=primary_unit_name)

    primary_hostname = hostname_from_unit(juju, primary_unit_name)
    machine_name = primary_hostname
    if substrate == Substrate.K8S:
        primary_hostname = f"{primary_hostname}.{APP_NAME}-endpoints"

    logger.info("Identified container name for primary unit: %s", primary_hostname)
    cut_network_from_unit(substrate, juju.model, machine_name, change_ip=change_ip)

    # on K8s the controller is on a different namespace
    if substrate == Substrate.VM:
        controller_hostname = lxd_get_controller_hostname(juju)
        assert not is_unit_reachable(
            juju, controller_hostname, primary_hostname, substrate, number_of_retries=3
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

    logger.info(
        "Network successfully cut to primary unit %s at %s. Verifying new primary election...",
        primary_unit_name,
        old_primary_endpoint,
    )

    new_primary_endpoint = None
    for attempt in Retrying(stop=stop_after_attempt(10), wait=wait_fixed(10)):
        with attempt:
            try:
                new_primary_endpoint = get_primary_ip(juju, APP_NAME, tls_enabled=tls_enabled)
                break
            except ValueError as e:
                logger.warning(f"Error getting primary IP after network cut: {e}")
            logger.info("Waiting for new primary to be elected...")

    assert new_primary_endpoint and new_primary_endpoint != old_primary_endpoint, (
        "Primary IP did not change after cutting network to the primary unit."
    )
    logger.info(
        "New primary IP after network cut: %s vs old primary IP: %s",
        new_primary_endpoint,
        old_primary_endpoint,
    )

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

    logger.info(
        "Verified that a new primary has been elected and is reachable at %s. Verifying that old primary endpoint is marked as down in sentinels of other units...",
        new_primary_endpoint,
    )
    for hostname in hostnames:
        if hostname == old_primary_endpoint:
            continue
        assert endpoint_in_sentinels(
            juju, old_primary_endpoint, hostname, status="s_down", tls_enabled=tls_enabled
        ), (
            f"The old primary endpoint should be marked as down in sentinels list of hostname {hostname} after network cut."
        )
        logger.info(
            "Verified that old primary endpoint %s is marked as down in sentinels of hostname %s after network cut.",
            old_primary_endpoint,
            hostname,
        )

    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=tls_enabled,
    )

    # restore network to the original primary unit
    logger.info("Restoring network to original primary unit at %s", primary_hostname)
    restore_network_to_unit(substrate, juju.model, machine_name, change_ip=change_ip)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        )
    )
    c_writes.update()

    logger.info(
        "Network restored to original primary unit %s. Verifying that all units can reach the original primary unit at %s...",
        primary_unit_name,
        primary_hostname,
    )
    for unit in juju.status().apps[APP_NAME].units:
        if unit == primary_unit_name:
            continue
        assert is_unit_reachable(
            juju, hostname_from_unit(juju, unit), primary_hostname, substrate
        ), (
            f"Unit {unit} cannot reach the original primary unit {primary_hostname} after network restoration."
        )
        logger.info(
            "Unit %s can reach the original primary unit %s after network restoration.",
            unit,
            primary_hostname,
        )

    download_client_certificate_from_unit(juju, APP_NAME, unit_name=primary_unit_name)
    new_unit_ip = get_ip_from_unit(juju, primary_unit_name)
    # read ip from cert and check if is a different ip than before if change_ip is True
    certificate_sans = get_sans_from_certificate("./client.pem")
    if change_ip:
        assert old_primary_endpoint not in certificate_sans["sans_ip"], (
            "The old IP should not be in SANs of client certificate after network cut and IP change."
        )
        assert new_unit_ip in certificate_sans["sans_ip"], (
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

    # only on lxd
    for hostname in hostnames:
        if hostname == new_unit_ip:
            continue
        if change_ip:
            assert not endpoint_in_sentinels(
                juju, old_primary_endpoint, hostname, tls_enabled=tls_enabled
            ), (
                f"The old primary endpoint should not be present in sentinels list of hostname {hostname} after network cut and IP change."
            )
            assert endpoint_in_sentinels(juju, new_unit_ip, hostname, tls_enabled=tls_enabled), (
                f"The new primary IP should be present in sentinels list of hostname {hostname} after network cut and IP change."
            )
            logger.info(
                "Verified that old primary endpoint %s is not in sentinels and new primary IP %s is in sentinels of hostname %s after network restoration with IP change.",
                old_primary_endpoint,
                new_unit_ip,
                hostname,
            )
        else:
            assert endpoint_in_sentinels(
                juju, old_primary_endpoint, hostname, tls_enabled=tls_enabled
            ), (
                f"The old primary endpoint should be present in sentinels list of hostname {hostname} after network cut and no IP change."
            )
            logger.info(
                "Verified that old primary endpoint %s is in sentinels of hostname %s after network restoration with no IP change.",
                old_primary_endpoint,
                hostname,
            )

    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        tls_enabled=tls_enabled,
    )
