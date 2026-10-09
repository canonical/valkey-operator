#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from time import sleep

import jubilant

from src.literals import CharmUsers, Substrate
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
    configure_cw_runner,
    start_continuous_writes,
    stop_continuous_writes,
)
from tests.integration.helpers import (
    APP_NAME,
    DATA_INTEGRATOR_NAME,
    DEPLOY_TIMEOUT_S,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    get_cluster_addresses,
    get_password,
    leader_unit_name,
)
from tests.integration.upgrades.literals import (
    CHARM_BASE,
    CHARM_CHANNEL,
    GLIDE_RUNNER_NAME,
    NUM_UNITS,
)

logger = logging.getLogger(__name__)


def test_deploy(juju: jubilant.Juju, substrate: Substrate, glide_runner_charm: str) -> None:
    """Deploy the charm with the previous version."""
    juju.deploy(
        APP_NAME,
        num_units=NUM_UNITS,
        channel=CHARM_CHANNEL,
        base=CHARM_BASE,
        config={"pause-after-unit-refresh": "all"},
        trust=True,
    )
    juju.deploy(glide_runner_charm, GLIDE_RUNNER_NAME)
    juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
    juju.deploy(
        DATA_INTEGRATOR_NAME,
        channel="latest/edge",
        config={"prefix-name": "my-keys:"},
    )
    juju.integrate(f"{APP_NAME}:valkey-client", f"{DATA_INTEGRATOR_NAME}:valkey")

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )


def test_upgrade_enable_tls(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Refresh the charm and enable TLS while upgrading."""
    leader_unit = leader_unit_name(juju)
    logger.info("Running `pre-refresh-check` action")
    pre_refresh_response = juju.run(leader_unit, "pre-refresh-check")
    assert pre_refresh_response.return_code == 0, "action failed"

    # Refresh always happens from highest to lowest unit number
    refresh_order = sorted(
        juju.status().get_units(APP_NAME),
        key=lambda unit_name: int(unit_name.split("/")[1]),
        reverse=True,
    )

    # initiate the upgrade
    logger.info("Refresh Valkey")
    juju.refresh(
        app=APP_NAME,
        path=charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
    )
    logger.info("Wait for the refresh to initiate")
    sleep(90)

    # versions will always be marked "incompatible" if refresh to a local version
    if (
        "incompatible" in juju.status().apps.get(APP_NAME).app_status.message
        or "incompatible"
        in juju.status().get_units(APP_NAME)[refresh_order[0]].workload_status.message
    ):
        logger.info("Upgrade is blocked due to incompatibility")

        logger.info(f"Continue refresh on unit {refresh_order[0]}")
        logger.info("Running `force-refresh-start` action with check-compatibility=false")
        force_refresh_response = juju.run(
            refresh_order[0],
            "force-refresh-start",
            {"check-compatibility": False, "run-pre-refresh-checks": False},
        )
        assert force_refresh_response.return_code == 0, "action failed"

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS)
    )

    assert "resume-refresh" in juju.status().apps.get(APP_NAME).app_status.message, (
        "Refresh should wait for user to continue with `resume-refresh` action"
    )

    logger.info("Enable TLS while upgrade is in progress")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS)
    )

    logger.info("Starting continuous writes")
    configure_cw_runner(
        juju,
        valkey_app=APP_NAME,
        tls_enabled=True,
        substrate=substrate,
    )
    start_continuous_writes(juju, clear=True)

    assert "resume-refresh" in juju.status().apps.get(APP_NAME).app_status.message, (
        "Refresh should wait for user to continue with `resume-refresh` action"
    )

    logger.info("Continue refresh on the next units with `resume-refresh` action")
    resume_unit = leader_unit_name(juju) if substrate == Substrate.K8S else refresh_order[1]
    try:
        juju.run(resume_unit, "resume-refresh")
    except jubilant.TaskError as e:
        if "terminated" in e.task.message:
            logger.info("Unit already terminated before action completed")
        else:
            raise

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS)
    )

    assert "resume-refresh" in juju.status().apps.get(APP_NAME).app_status.message, (
        "Refresh should wait for user to continue with `resume-refresh` action"
    )

    assert_continuous_writes_increasing(juju)
    stats = stop_continuous_writes(juju)

    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=stats.last_written_value,
        tls_enabled=True,
    )


def test_remove_client_relation(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Remove a client relation while upgrading."""
    logger.info("Starting continuous writes")
    configure_cw_runner(
        juju,
        valkey_app=APP_NAME,
        tls_enabled=True,
        substrate=substrate,
    )
    start_continuous_writes(juju, clear=True)

    assert "resume-refresh" in juju.status().apps.get(APP_NAME).app_status.message, (
        "Refresh should wait for user to continue with `resume-refresh` action"
    )
    logger.info("Remove the client relations")
    juju.remove_relation(f"{APP_NAME}:valkey-client", f"{DATA_INTEGRATOR_NAME}:valkey")

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS)
    )

    assert "resume-refresh" in juju.status().apps.get(APP_NAME).app_status.message, (
        "Refresh should wait for user to continue with `resume-refresh` action"
    )

    assert_continuous_writes_increasing(juju)
    stats = stop_continuous_writes(juju)

    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=stats.last_written_value,
        tls_enabled=True,
    )


def test_scale_up_during_upgrade(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Add a unit while upgrading."""
    start_continuous_writes(juju, clear=True)
    logger.info("Scale up while upgrade is in progress")
    juju.add_unit(APP_NAME, num_units=1)
    num_units = NUM_UNITS + 1
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=num_units)
    )

    assert "resume-refresh" in juju.status().apps.get(APP_NAME).app_status.message, (
        "Refresh should wait for user to continue with `resume-refresh` action"
    )

    logger.info("Continue refresh on the last units with `resume-refresh` action")
    # Refresh always happens from highest to lowest unit number
    refresh_order = sorted(
        juju.status().get_units(APP_NAME),
        key=lambda unit_name: int(unit_name.split("/")[1]),
        reverse=True,
    )
    resume_unit = leader_unit_name(juju) if substrate == Substrate.K8S else refresh_order[-1]
    try:
        juju.run(resume_unit, "resume-refresh")
    except jubilant.TaskError as e:
        if "terminated" in e.task.message:
            logger.info("Unit already terminated before action completed")
        else:
            raise

    logger.info("Wait for upgrade to complete")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=num_units, idle_period=30
        )
    )

    assert_continuous_writes_increasing(juju)
    stats = stop_continuous_writes(juju)

    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=stats.last_written_value,
        tls_enabled=True,
    )
