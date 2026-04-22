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
    are_apps_active_and_agents_idle,
    existing_app,
    get_cluster_addresses,
    get_number_connected_replicas,
    get_password,
    get_primary_ip,
    get_quorum,
    remove_number_units,
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
    if existing_app(juju):
        return

    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=1,
        trust=True,
    )
    juju.deploy(glide_runner_charm, app=GLIDE_RUNNER_NAME)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, GLIDE_RUNNER_NAME, idle_period=30
        ),
        timeout=600,
    )

    assert len(juju.status().apps[APP_NAME].units) == 1, (
        "Unexpected number of units after initial deploy"
    )


def test_seed_data(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Seed some data to the cluster."""
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


def test_check_quorum(juju: jubilant.Juju) -> None:
    """Check quorum value."""
    app_name = existing_app(juju) or APP_NAME
    init_units_count = len(juju.status().apps[app_name].units)
    assert get_quorum(juju, f"{app_name}/0") == (init_units_count // 2) + 1, (
        "Unexpected quorum value after initial deploy"
    )


def test_scale_up(juju: jubilant.Juju, glide_runner, substrate: Substrate) -> None:
    """Make sure new units are added to the valkey downtime."""
    app_name = existing_app(juju) or APP_NAME
    init_units_count = len(juju.status().apps[app_name].units)
    configure_cw_runner(juju, valkey_app=app_name, substrate=substrate)
    start_continuous_writes(juju, clear=True)

    # scale up
    juju.add_unit(app_name, num_units=2)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, app_name, idle_period=10, unit_count=init_units_count + 2
        ),
        timeout=1200,
    )
    num_units = len(juju.status().apps[app_name].units)
    assert num_units == init_units_count + 2, (
        f"Expected {init_units_count + 2} units, got {num_units}."
    )

    for unit in juju.status().apps[app_name].units:
        assert get_quorum(juju, unit) == (num_units // 2) + 1, (
            f"Unexpected quorum value for unit {unit} after scale up"
        )

    # check if all units have been added to the cluster
    addresses = get_cluster_addresses(juju, app_name)

    connected_replicas = get_number_connected_replicas(juju)
    assert connected_replicas == init_units_count + 1, (
        f"Expected {init_units_count + 1} connected replicas, got {connected_replicas}."
    )

    assert_continuous_writes_increasing(juju)
    logger.info("Stopping continuous writes after scale up test.")
    cw_stats = stop_continuous_writes(juju)
    assert_continuous_writes_consistent(
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )


def test_scale_down_one_unit(juju: jubilant.Juju, substrate: Substrate, glide_runner) -> None:
    """Make sure scale down operations complete successfully."""
    app_name = existing_app(juju) or APP_NAME
    init_units_count = len(juju.status().apps[app_name].units)

    if init_units_count < NUM_UNITS:
        juju.add_unit(app_name, num_units=NUM_UNITS - init_units_count)
        init_units_count = NUM_UNITS
        juju.wait(
            lambda status: are_apps_active_and_agents_idle(
                status, app_name, idle_period=10, unit_count=init_units_count
            ),
            timeout=1200,
        )

    number_of_replicas = get_number_connected_replicas(juju)
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} connected replicas, got {number_of_replicas}."
    )

    configure_cw_runner(juju, valkey_app=app_name, substrate=substrate)
    start_continuous_writes(juju, clear=True)
    sleep(10)  # let the continuous writes write some data

    # scale down
    remove_number_units(juju, app_name, num_units=1, substrate=substrate)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, app_name, unit_count=init_units_count - 1, idle_period=10
        )
    )
    num_units = len(juju.status().get_units(app_name))
    assert num_units == init_units_count - 1, (
        f"Expected {init_units_count - 1} units, got {num_units}."
    )

    for unit in juju.status().apps[app_name].units:
        assert get_quorum(juju, unit) == (num_units // 2) + 1, (
            f"Unexpected quorum value for unit {unit} after scale down"
        )

    number_of_replicas = get_number_connected_replicas(juju)
    assert number_of_replicas == init_units_count - 2, (
        f"Expected {init_units_count - 2} connected replicas, got {number_of_replicas}."
    )

    # update hostnames after scale down
    configure_cw_runner(juju, valkey_app=app_name, substrate=substrate)

    assert_continuous_writes_increasing(juju)

    logger.info("Stopping continuous writes after scale down test.")
    cw_stats = stop_continuous_writes(juju)
    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, app_name),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )


def test_scale_down_multiple_units(
    juju: jubilant.Juju, substrate: Substrate, glide_runner
) -> None:
    """Make sure multiple scale down operations complete successfully."""
    app_name = existing_app(juju) or APP_NAME
    init_units_count = len(juju.status().apps[app_name].units)
    if init_units_count < NUM_UNITS + 1:
        juju.add_unit(app_name, num_units=(NUM_UNITS + 1) - init_units_count)
        juju.wait(
            lambda status: are_apps_active_and_agents_idle(
                status, app_name, idle_period=10, unit_count=NUM_UNITS + 1
            ),
            timeout=1200,
        )
        init_units_count = NUM_UNITS + 1

    number_of_replicas = get_number_connected_replicas(juju)
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} connected replicas, got {number_of_replicas}."
    )

    configure_cw_runner(juju, valkey_app=app_name, substrate=substrate)
    start_continuous_writes(juju, clear=True)

    sleep(10)  # let the continuous writes write some data

    # scale down multiple units
    remove_number_units(juju, app_name, num_units=2, substrate=substrate)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, app_name, unit_count=init_units_count - 2, idle_period=10
        )
    )
    num_units = len(juju.status().get_units(app_name))
    assert num_units == init_units_count - 2, (
        f"Expected {init_units_count - 2} units, got {num_units}."
    )

    number_of_replicas = get_number_connected_replicas(juju)
    assert number_of_replicas == init_units_count - 3, (
        f"Expected {init_units_count - 3} connected replicas, got {number_of_replicas}."
    )

    for unit in juju.status().apps[app_name].units:
        assert get_quorum(juju, unit) == (num_units // 2) + 1, (
            f"Unexpected quorum value for unit {unit} after scale down"
        )

    configure_cw_runner(
        juju, valkey_app=app_name, substrate=substrate
    )  # update hostnames after scale down

    assert_continuous_writes_increasing(juju)

    logger.info("Stopping continuous writes after scale down test.")
    cw_stats = stop_continuous_writes(juju)

    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, app_name),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )


def test_scale_down_to_zero_and_back_up(
    juju: jubilant.Juju, substrate: Substrate, glide_runner
) -> None:
    """Make sure that removing all units and then adding them again works."""
    app_name = existing_app(juju) or APP_NAME
    # remove all remaining units
    remove_number_units(
        juju, app_name, num_units=len(juju.status().apps[app_name].units), substrate=substrate
    )
    juju.wait(lambda status: len(juju.status().get_units(app_name)) == 0)

    # scale up again
    juju.add_unit(app_name, num_units=NUM_UNITS)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, app_name, unit_count=NUM_UNITS, idle_period=10
        ),
        timeout=1200,
    )

    addresses = get_cluster_addresses(juju, app_name)

    connected_replicas = get_number_connected_replicas(juju)
    assert connected_replicas == NUM_UNITS - 1, (
        f"Expected {NUM_UNITS - 1} connected replicas, got {connected_replicas}."
    )

    configure_cw_runner(juju, valkey_app=app_name, substrate=substrate)
    start_continuous_writes(juju, clear=True)

    sleep(10)  # let the continuous writes write some data
    assert_continuous_writes_increasing(juju)

    logger.info("Stopping continuous writes after scale up test.")
    cw_stats = stop_continuous_writes(juju)

    assert_continuous_writes_consistent(
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )


def test_scale_down_primary(juju: jubilant.Juju, substrate: Substrate, glide_runner) -> None:
    """Make sure that removing the primary unit triggers a new primary to be elected and the cluster remains available."""
    if substrate == Substrate.K8S:
        pytest.skip("Primary unit can only targeted on VM")

    app_name = existing_app(juju) or APP_NAME
    init_units_count = len(juju.status().apps[app_name].units)
    if init_units_count < NUM_UNITS:
        juju.add_unit(app_name, num_units=NUM_UNITS - init_units_count)
        juju.wait(
            lambda status: are_apps_active_and_agents_idle(
                status, app_name, idle_period=10, unit_count=NUM_UNITS
            ),
            timeout=1200,
        )
        init_units_count = NUM_UNITS

    configure_cw_runner(juju, valkey_app=app_name, substrate=substrate)
    start_continuous_writes(juju, clear=True)
    sleep(10)  # let the continuous writes write some data

    primary_endpoint = get_primary_ip(juju, app_name)
    primary_unit = next(
        unit
        for unit, unit_value in juju.status().get_units(app_name).items()
        if unit_value.public_address == primary_endpoint
    )
    assert primary_unit is not None, "Failed to identify primary unit for scale down test."
    logger.info(
        "Identified primary unit %s with endpoint %s for scale down test.",
        primary_unit,
        primary_endpoint,
    )
    juju.remove_unit(primary_unit)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, app_name, unit_count=init_units_count - 1, idle_period=10
        )
    )
    configure_cw_runner(
        juju, valkey_app=app_name, substrate=substrate
    )  # update hostnames after primary unit removal
    new_primary_endpoint = get_primary_ip(juju, app_name)
    assert new_primary_endpoint != primary_endpoint, (
        "Primary endpoint did not change after removing primary unit."
    )
    logger.info(f"New primary endpoint after scale down is {new_primary_endpoint}.")
    endpoints = get_cluster_addresses(juju, app_name)
    assert_continuous_writes_increasing(juju)
    cw_stats = stop_continuous_writes(juju)
    assert_continuous_writes_consistent(
        endpoints=endpoints,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=cw_stats.last_written_value,
    )


def test_scale_down_remove_application(juju: jubilant.Juju) -> None:
    """Make sure the application can be removed."""
    juju.remove_application(APP_NAME)

    juju.wait(
        lambda status: APP_NAME not in status.apps,
        timeout=600,
        delay=5,
    )
