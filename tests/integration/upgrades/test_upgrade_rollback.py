#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from platform import machine
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
from tests.integration.upgrades.literals import (
    CHARM_CHANNEL,
    CHARM_REVISIONS_TO_DEPLOY,
    GLIDE_RUNNER_NAME,
    NUM_UNITS,
    WORKLOAD_VERSION,
)

from ..helpers import (
    APP_NAME,
    DEPLOY_TIMEOUT_S,
    IMAGE_RESOURCE,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    get_cluster_addresses,
    get_password,
    leader_unit_name,
)

logger = logging.getLogger(__name__)


def test_deploy(juju: jubilant.Juju, substrate: Substrate, glide_runner_charm: str) -> None:
    """Deploy the charm with the previous version."""
    juju.deploy(
        APP_NAME,
        num_units=NUM_UNITS,
        channel=CHARM_CHANNEL,
        revision=CHARM_REVISIONS_TO_DEPLOY[machine()],
        trust=True,
        config={"pause-after-unit-refresh": "all"},
    )
    juju.deploy(glide_runner_charm, GLIDE_RUNNER_NAME)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, GLIDE_RUNNER_NAME, unit_count=NUM_UNITS, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )


def test_rollback(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Run a refresh, fail and roll back."""
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

    logger.info("Initiate refresh")
    juju.refresh(
        app=APP_NAME,
        path=charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
    )

    if "incompatible" in juju.status().apps.get(APP_NAME).app_status.message:
        logger.info("Upgrade is blocked due to incompatibility")

        logger.info(f"Continue refresh on unit {refresh_order[0]}")
        logger.info("Running `force-refresh-start` action with check-compatibility=false")
        juju.run(
            refresh_order[0],
            "force-refresh-start",
            params={"check-compatibility": False, "run-pre-refresh-checks": False},
            wait=1200,
        )

    # wait for the first refreshed unit to settle
    juju.wait(lambda status: are_agents_idle(status, APP_NAME, unit_count=NUM_UNITS))

    # in `juju refresh`, --switch and --revision are mutually exclusive
    # we can only roll back to the latest released revision from a local charm
    refresh_cmd = (
        f"refresh {APP_NAME} --model={juju.model} --switch {APP_NAME} --channel {CHARM_CHANNEL}"
    )
    juju.cli(
        *refresh_cmd.split(),
        include_model=False,
    )

    juju.wait(lambda status: are_agents_idle(status, APP_NAME, idle_period=60))

    if "incompatible" in juju.status().apps.get(APP_NAME).app_status.message:
        # will be marked "incompatible" if rollback is not to the same revision as initially deployed
        logger.info("Rollback is blocked due to incompatibility")

        logger.info("Running `force-refresh-start` action with check-compatibility=false")
        juju.run(refresh_order[0], "force-refresh-start", {"check-compatibility": False})
    elif "Refreshing" in juju.status().apps.get(APP_NAME).app_status.message:
        # rolling back from local to published is only possible to the latest revision
        # if this is not run in a PR, the local built version is the same as the latest published
        # to roll back to the initially deployed version, we need to issue another rollback command
        logger.info("Rolling back to previous revision")
        juju.refresh(app=APP_NAME, revision=CHARM_REVISIONS_TO_DEPLOY[machine()])

    # wait for rollback to complete
    assert_continuous_writes_increasing(juju)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        ),
    )

    stats = stop_continuous_writes(juju)

    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=stats.last_written_value,
        tls_enabled=False,
    )


def test_upgrade_to_local(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Refresh the charm and upgrade etcd, ensuring high availability while upgrading."""
    juju.config(APP_NAME, {"pause-after-unit-refresh": "all"})
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        ),
    )

    configure_cw_runner(
        juju,
        valkey_app=APP_NAME,
        tls_enabled=False,
        substrate=substrate,
    )
    start_continuous_writes(juju, clear=True)

    # pre-refresh-check
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
    logger.info(f"Refresh Valkey to v{WORKLOAD_VERSION['target']}")
    juju.refresh(
        app=APP_NAME,
        path=charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
    )
    logger.info("Wait for the refresh to initiate")
    sleep(90)

    # versions will always be marked "incompatible" if refresh to a local version
    # this will not be the case when the PR is released
    # see: https://github.com/canonical/charm-refresh/blob/main/charm_refresh/_main.py#L182-L185
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=60, unit_count=NUM_UNITS)
    )

    if "incompatible" in juju.status().apps.get(APP_NAME).app_status.message:
        logger.info("Upgrade is blocked due to incompatibility")

        logger.info(f"Continue refresh on unit {refresh_order[0]}")
        logger.info("Running `force-refresh-start` action with check-compatibility=false")
        force_refresh_response = juju.run(
            refresh_order[0], "force-refresh-start", {"check-compatibility": False}
        )
        assert force_refresh_response.return_code == 0, "action failed"

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS)
    )
    assert_continuous_writes_increasing(juju)

    assert "resume-refresh" in juju.status().apps.get(APP_NAME).app_status.message, (
        "Refresh should wait for user to continue with `resume-refresh` action"
    )

    logger.info("Continue refresh on all other units with `resume-refresh` action")
    resume_refresh_response = juju.run(refresh_order[1], "resume-refresh")
    assert resume_refresh_response.return_code == 0, "action failed"

    # wait for upgrade to complete
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, unit_count=NUM_UNITS)
    )
    assert_continuous_writes_increasing(juju)

    stats = stop_continuous_writes(juju)

    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        last_written_value=stats.last_written_value,
        tls_enabled=False,
    )
