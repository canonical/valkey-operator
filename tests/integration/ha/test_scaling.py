#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import asyncio
import logging

import jubilant
import pytest

from literals import CharmUsers, Substrate
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
)
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
    get_cluster_hostnames,
    get_number_connected_replicas,
    get_password,
    get_primary_ip,
    remove_number_units,
    seed_valkey,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"


def test_build_and_deploy(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=1,
        trust=True,
    )
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    assert len(juju.status().apps[APP_NAME].units) == 1, (
        "Unexpected number of units after initial deploy"
    )


async def test_seed_data(juju: jubilant.Juju) -> None:
    """Seed some data to the cluster."""
    await seed_valkey(juju, target_gb=1)


async def test_scale_up(juju: jubilant.Juju, c_writes, c_writes_runner) -> None:
    """Make sure new units are added to the valkey downtime."""
    init_units_count = len(juju.status().apps[APP_NAME].units)

    # scale up
    juju.add_unit(APP_NAME, num_units=NUM_UNITS - init_units_count)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, idle_period=10, unit_count=NUM_UNITS
        ),
        timeout=1200,
    )
    num_units = len(juju.status().apps[APP_NAME].units)
    assert num_units == NUM_UNITS, f"Expected {NUM_UNITS} units, got {num_units}."

    # check if all units have been added to the cluster
    hostnames = get_cluster_hostnames(juju, APP_NAME)

    connected_replicas = await get_number_connected_replicas(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert connected_replicas == NUM_UNITS - 1, (
        f"Expected {NUM_UNITS - 1} connected replicas, got {connected_replicas}."
    )

    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    logger.info("Stopping continuous writes after scale up test.")
    logger.info(await c_writes.async_stop())
    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )


async def test_scale_down_one_unit(juju: jubilant.Juju, substrate: Substrate, c_writes) -> None:
    """Make sure scale down operations complete successfully."""
    number_of_replicas = await get_number_connected_replicas(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert number_of_replicas == NUM_UNITS - 1, (
        f"Expected {NUM_UNITS - 1} connected replicas, got {number_of_replicas}."
    )

    await c_writes.async_clear()
    c_writes.start()
    await asyncio.sleep(10)  # let the continuous writes write some data

    # scale down
    remove_number_units(juju, APP_NAME, num_units=1, substrate=substrate)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS - 1, idle_period=10
        )
    )
    num_units = len(juju.status().get_units(APP_NAME))
    assert num_units == NUM_UNITS - 1, f"Expected {NUM_UNITS - 1} units, got {num_units}."

    number_of_replicas = await get_number_connected_replicas(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert number_of_replicas == NUM_UNITS - 2, (
        f"Expected {NUM_UNITS - 2} connected replicas, got {number_of_replicas}."
    )

    # update hostnames after scale down
    c_writes.update()

    await assert_continuous_writes_increasing(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )

    logger.info("Stopping continuous writes after scale up test.")
    logger.info(await c_writes.async_stop())

    assert_continuous_writes_consistent(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )


async def test_scale_down_multiple_units(
    juju: jubilant.Juju, substrate: Substrate, c_writes
) -> None:
    """Make sure multiple scale down operations complete successfully."""
    number_current_units = len(juju.status().apps[APP_NAME].units)
    juju.add_unit(APP_NAME, num_units=(NUM_UNITS + 1) - number_current_units)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, idle_period=10, unit_count=NUM_UNITS + 1
        ),
        timeout=1200,
    )

    number_of_replicas = await get_number_connected_replicas(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert number_of_replicas == NUM_UNITS, (
        f"Expected {NUM_UNITS} connected replicas, got {number_of_replicas}."
    )

    await c_writes.async_clear()
    c_writes.start()
    await asyncio.sleep(10)  # let the continuous writes write some data

    # scale down multiple units
    remove_number_units(juju, APP_NAME, num_units=2, substrate=substrate)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS - 1, idle_period=10
        )
    )
    num_units = len(juju.status().get_units(APP_NAME))
    assert num_units == NUM_UNITS - 1, f"Expected {NUM_UNITS - 1} units, got {num_units}."

    number_of_replicas = await get_number_connected_replicas(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert number_of_replicas == NUM_UNITS - 2, (
        f"Expected {NUM_UNITS - 2} connected replicas, got {number_of_replicas}."
    )

    c_writes.update()

    await assert_continuous_writes_increasing(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )

    logger.info("Stopping continuous writes after scale down test.")
    logger.info(await c_writes.async_stop())

    assert_continuous_writes_consistent(
        hostnames=get_cluster_hostnames(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )


async def test_scale_down_to_zero_and_back(
    juju: jubilant.Juju, substrate: Substrate, c_writes
) -> None:
    """Make sure that removing all units and then adding them again works."""
    # remove all remaining units
    remove_number_units(
        juju, APP_NAME, num_units=len(juju.status().apps[APP_NAME].units), substrate=substrate
    )
    juju.wait(lambda status: len(juju.status().get_units(APP_NAME)) == 0)

    # scale up again
    juju.add_unit(APP_NAME, num_units=NUM_UNITS)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=10
        ),
        timeout=1200,
    )

    hostnames = get_cluster_hostnames(juju, APP_NAME)

    connected_replicas = await get_number_connected_replicas(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert connected_replicas == NUM_UNITS - 1, (
        f"Expected {NUM_UNITS - 1} connected replicas, got {connected_replicas}."
    )
    await c_writes.async_clear()
    c_writes.start()
    await asyncio.sleep(10)  # let the continuous writes write some data
    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    logger.info("Stopping continuous writes after scale up test.")
    logger.info(await c_writes.async_stop())
    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    await c_writes.async_clear()


async def test_scale_down_primary(juju: jubilant.Juju, substrate: Substrate, c_writes) -> None:
    """Make sure that removing the primary unit triggers a new primary to be elected and the cluster remains available."""
    if substrate == Substrate.K8S:
        pytest.skip("Primary unit can only targeted on VM")

    await c_writes.async_clear()
    c_writes.start()
    primary_ip = get_primary_ip(juju, APP_NAME)
    primary_unit = next(
        unit
        for unit, unit_value in juju.status().get_units(APP_NAME).items()
        if unit_value.public_address == primary_ip
    )
    assert primary_unit is not None, "Failed to identify primary unit for scale down test."
    logger.info(
        f"Identified primary unit {primary_unit} with IP {primary_ip} for scale down test."
    )
    juju.remove_unit(primary_unit)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS - 1, idle_period=10
        )
    )
    c_writes.update()
    new_primary_ip = get_primary_ip(juju, APP_NAME)
    assert new_primary_ip != primary_ip, "Primary IP did not change after removing primary unit."
    logger.info(f"New primary IP after scale down is {new_primary_ip}.")
    hostnames = get_cluster_hostnames(juju, APP_NAME)
    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    logger.info("Stopping continuous writes after primary scale down test.")
    logger.info(await c_writes.async_stop())
    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    await c_writes.async_clear()


def test_scale_down_remove_application(juju: jubilant.Juju) -> None:
    """Make sure the application can be removed."""
    juju.remove_application(APP_NAME)

    juju.wait(
        lambda status: APP_NAME not in status.apps,
        timeout=600,
        delay=5,
    )
