#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import asyncio
import logging

import jubilant

from literals import Substrate
from tests.integration.ha.helpers.helpers import (
    cut_network_from_unit,
    get_unit_name_from_primary_ip,
    hostname_from_unit,
    is_unit_reachable,
    restore_network_to_unit,
)
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    CharmUsers,
    are_apps_active_and_agents_idle,
    get_cluster_hostnames,
    get_number_connected_replicas,
    get_password,
    get_primary_ip,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3


def test_build_and_deploy(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    assert len(juju.status().apps[APP_NAME].units) == NUM_UNITS, (
        f"Unexpected number of units after initial deploy: expected {NUM_UNITS}, got {len(juju.status().apps[APP_NAME].units)}"
    )


async def test_network_cut_primary(juju: jubilant.Juju, substrate: Substrate, chaos_mesh) -> None:
    """Cut the network to the primary unit and verify that a new primary is elected."""
    # Get the current primary unit
    primary_ip = get_primary_ip(juju, APP_NAME)
    assert primary_ip, "Failed to get primary endpoint from Juju status."

    # Cut the network to the primary unit
    logger.info("Cutting network to primary unit at %s", primary_ip)
    primary_unit_name = get_unit_name_from_primary_ip(juju, primary_ip, substrate)
    primary_hostname = hostname_from_unit(juju, primary_unit_name)
    machine_name = primary_hostname
    if substrate == Substrate.K8S:
        primary_hostname = f"{primary_hostname}.{APP_NAME}-endpoints"
    logger.info("Identified container name for primary unit: %s", primary_hostname)
    cut_network_from_unit(juju, substrate, machine_name)

    for unit in juju.status().apps[APP_NAME].units:
        if unit == primary_unit_name:
            continue
        assert not is_unit_reachable(
            hostname_from_unit(juju, unit), primary_hostname, substrate
        ), f"Unit {unit} can still reach the primary unit {primary_hostname} after network cut."

    logger.info(
        "Network successfully cut to primary unit %s at %s. Verifying new primary election...",
        primary_unit_name,
        primary_ip,
    )
    while True:
        try:
            new_primary_ip = get_primary_ip(juju, APP_NAME)
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

    # check replica number that it is down to NUM_UNITS - 2
    number_of_replicas = await get_number_connected_replicas(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert number_of_replicas == NUM_UNITS - 2, (
        f"Expected {NUM_UNITS - 2} connected replicas, got {number_of_replicas}."
    )

    # restore network to the original primary unit
    logger.info("Restoring network to original primary unit at %s", primary_hostname)
    restore_network_to_unit(juju, substrate, machine_name)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        )
    )

    for unit in juju.status().apps[APP_NAME].units:
        if unit == primary_unit_name:
            continue
        assert is_unit_reachable(hostname_from_unit(juju, unit), primary_hostname, substrate), (
            f"Unit {unit} cannot reach the original primary unit {primary_hostname} after network restoration."
        )

    # check replica number that it is back to NUM_UNITS - 1
    number_of_replicas = await get_number_connected_replicas(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert number_of_replicas == NUM_UNITS - 1, (
        f"Expected {NUM_UNITS - 1} connected replicas after network restoration, got {number_of_replicas}."
    )
