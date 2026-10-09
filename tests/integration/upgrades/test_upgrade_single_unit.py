#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from platform import machine
from time import sleep

import jubilant

from src.literals import Substrate
from tests.integration.cw_helpers import (
    assert_continuous_writes_increasing,
    configure_cw_runner,
    start_continuous_writes,
    stop_continuous_writes,
)
from tests.integration.helpers import (
    APP_NAME,
    DEPLOY_TIMEOUT_S,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
)
from tests.integration.upgrades.literals import (
    CHARM_CHANNEL,
    CHARM_REVISIONS_TO_DEPLOY,
    GLIDE_RUNNER_NAME,
)

NUM_UNITS = 1

logger = logging.getLogger(__name__)


def test_deploy(juju: jubilant.Juju, substrate: Substrate, glide_runner_charm: str) -> None:
    """Deploy the charm with the previous version."""
    juju.deploy(
        APP_NAME,
        num_units=NUM_UNITS,
        channel=CHARM_CHANNEL,
        revision=CHARM_REVISIONS_TO_DEPLOY[machine()],
        trust=True,
    )
    juju.deploy(glide_runner_charm, GLIDE_RUNNER_NAME)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )


def test_upgrade_single_unit(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Refresh the charm and upgrade etcd, ensuring high availability while upgrading."""
    logger.info("Starting continuous writes")
    configure_cw_runner(
        juju,
        valkey_app=APP_NAME,
        tls_enabled=False,
        substrate=substrate,
    )
    start_continuous_writes(juju, clear=True)

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

    logger.info("Wait for upgrade to complete")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        )
    )
    assert_continuous_writes_increasing(juju)
    stop_continuous_writes(juju)
