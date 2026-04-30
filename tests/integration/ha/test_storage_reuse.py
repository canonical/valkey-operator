#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from time import sleep

import jubilant
import pytest

from literals import CharmUsers, Substrate
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
    configure_cw_runner,
    start_continuous_writes,
    stop_continuous_writes,
)
from tests.integration.helpers import (
    APP_NAME,
    GLIDE_RUNNER_NAME,
    IMAGE_RESOURCE,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    get_cluster_addresses,
    get_password,
    get_storage_id,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"
SEED_KEY_PREFIX = "seed:key:"


def test_build_and_deploy(
    charm: str, juju: jubilant.Juju, substrate: Substrate, glide_runner_charm
) -> None:
    """Build the charm-under-test and deploy it with three units."""
    if substrate == Substrate.VM:
        logger.info("Create storage pool on VM")
        juju.cli("create-storage-pool", "valkey-storage", "lxd")
        storage = {"data": "valkey-storage,2G"}
    else:
        storage = None

    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
        storage=storage,
    )
    juju.deploy(glide_runner_charm, app=GLIDE_RUNNER_NAME)
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
        timeout=600,
    )


def test_seed_data(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Seed some data to the cluster."""
    logger.info("Feed data into Valkey")
    configure_cw_runner(juju, substrate=substrate)
    task = juju.run(
        f"{GLIDE_RUNNER_NAME}/leader",
        "seed-data",
        params={
            "target-gb": 1.0,
            "key-prefix": SEED_KEY_PREFIX,
        },
    )
    if task.status != "completed":
        logger.error(f"Data seeding failed: {task.results}")


def test_attach_storage_after_scale_down(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Make sure storage can be re-attached after removing a unit."""
    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    start_continuous_writes(juju, clear=True)

    logger.info("Scale down while keeping the storage volume")
    unit = list(juju.status().get_units(APP_NAME))[-1]
    data_storage_id = get_storage_id(juju, unit, "data")
    if substrate == Substrate.VM:
        juju.remove_unit(unit)
    else:
        juju.remove_unit(APP_NAME, num_units=1)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS - 1, idle_period=60
        )
    )

    logger.info("Deploy new unit with the previous storage")
    juju.add_unit(
        APP_NAME,
        attach_storage=[data_storage_id] if substrate == Substrate.VM else None,
    )
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=60
        )
    )

    # update hostnames after scale down
    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)

    addresses = get_cluster_addresses(juju, APP_NAME)
    assert_continuous_writes_increasing(juju)

    logger.info("Stopping continuous writes")
    cw_stats = stop_continuous_writes(juju)
    assert_continuous_writes_consistent(
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )


def test_attach_storage_after_scale_to_zero(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Make sure storage can be re-attached after removing all units."""
    logger.info("Start writing data")
    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    start_continuous_writes(juju, clear=False)
    sleep(20)
    cw_stats = stop_continuous_writes(juju)

    logger.info("Remove all units while keeping their storage-ids for later reuse")
    storage_ids = []
    for unit in juju.status().get_units(APP_NAME):
        storage_ids.append(get_storage_id(juju, unit, "data"))
        # on VM, remove units one by one
        if substrate == Substrate.VM:
            juju.remove_unit(unit)

    if substrate == Substrate.K8S:
        juju.remove_unit(APP_NAME, num_units=NUM_UNITS)

    logger.info("Wait for all units to be gone")
    juju.wait(lambda status: len(juju.status().get_units(APP_NAME)) == 0)

    logger.info("Scale up again re-attaching the storage")
    for storage_id in storage_ids:
        juju.add_unit(APP_NAME, attach_storage=storage_id if substrate == Substrate.VM else None)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=60
        )
    )

    logger.info("Ensure previous data is available again")
    addresses = get_cluster_addresses(juju, APP_NAME)
    assert_continuous_writes_consistent(
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )

    logger.info("Restart continuous writes")
    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    start_continuous_writes(juju, clear=False)
    assert_continuous_writes_increasing(juju, wait=30)

    logger.info("Stopping continuous writes")
    cw_stats = stop_continuous_writes(juju)
    assert_continuous_writes_consistent(
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )


def test_attach_storage_after_removing_application(
    charm: str, juju: jubilant.Juju, substrate: Substrate
) -> None:
    """Make sure storage can be re-attached to a new application."""
    if substrate == Substrate.K8S:
        logger.info("This is currently not supported on Kubernetes.")
        return

    logger.info("Start writing data")
    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    start_continuous_writes(juju, clear=False)
    sleep(20)
    cw_stats = stop_continuous_writes(juju)

    logger.info("Remove all units except one")
    storage_ids = []
    for unit in list(juju.status().get_units(APP_NAME))[1:]:
        storage_ids.append(get_storage_id(juju, unit, "data"))
        juju.remove_unit(unit)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=1, idle_period=60
        )
    )

    logger.info("Remove the remaining unit after saving the storage id")
    unit = list(juju.status().get_units(APP_NAME))[0]
    storage_ids.append(get_storage_id(juju, unit, "data"))
    juju.remove_application(APP_NAME)
    juju.wait(lambda status: not juju.status().get_units(APP_NAME))

    logger.info("Deploy new application with previous storage")
    juju.deploy(charm, trust=True, attach_storage=storage_ids[-1])
    storage_ids.pop()
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=1, idle_period=60
        )
    )

    logger.info("Ensure previous data is available again")
    addresses = get_cluster_addresses(juju, APP_NAME)
    assert_continuous_writes_consistent(
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )

    logger.info("Restart continuous writes")
    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    start_continuous_writes(juju, clear=False)

    logger.info("Scale application with previous storage")
    for storage_id in storage_ids:
        juju.add_unit(APP_NAME, attach_storage=storage_id)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=60
        )
    )

    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    assert_continuous_writes_increasing(juju, wait=10)

    logger.info("Stopping continuous writes")
    cw_stats = stop_continuous_writes(juju)
    assert_continuous_writes_consistent(
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )
