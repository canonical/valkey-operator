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
    DEPLOY_TIMEOUT_S,
    IMAGE_RESOURCE,
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
        trust=True,
    )
    juju.deploy(glide_runner_charm, GLIDE_RUNNER_NAME)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
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

    logger.info("Initiate refresh")
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
        juju.run(
            refresh_order[0],
            "force-refresh-start",
            params={"check-compatibility": False, "run-pre-refresh-checks": False},
            wait=1200,
        )

    # wait for the first refreshed unit to settle
    juju.wait(lambda status: are_agents_idle(status, APP_NAME, unit_count=NUM_UNITS))

    # workaround until `workload_allowed_to_start` doesn't raise in `post_refresh_handling`
    # needs to be published to Charmhub before this can be removed
    # TODO: remove in a follow-up PR
    previous_resource = "valkey-image=ghcr.io/canonical/valkey-charmed@sha256:0799c89a3a2e55ce3978d18f690a2659fdb9ca2da7e5ec1747ea34985307853c"
    logger.info("Rolling back to previous revision")
    # in `juju refresh`, --switch and --revision are mutually exclusive
    # we can only roll back to the latest released revision from a local charm
    refresh_cmd = f"refresh {APP_NAME} --model={juju.model} --switch {APP_NAME} --channel {CHARM_CHANNEL} --resource {previous_resource}"
    juju.cli(
        *refresh_cmd.split(),
        include_model=False,
    )

    juju.wait(lambda status: are_agents_idle(status, APP_NAME, idle_period=60))

    if (
        "incompatible" in juju.status().apps.get(APP_NAME).app_status.message
        or "incompatible"
        in juju.status().get_units(APP_NAME)[refresh_order[0]].workload_status.message
    ):
        # will be marked "incompatible" if rollback is not to the same revision as initially deployed
        logger.info("Rollback is blocked due to incompatibility")

        logger.info("Running `force-refresh-start` action with check-compatibility=false")
        juju.run(
            refresh_order[0],
            "force-refresh-start",
            {"check-compatibility": False, "check-workload-container": False},
        )
    elif "Refreshing" in juju.status().apps.get(APP_NAME).app_status.message:
        # rolling back from local to published is only possible to the latest revision
        # if this is not run in a PR, the local built version is the same as the latest published
        # to roll back to the initially deployed version, we need to issue another rollback command
        logger.info("Rolling back to previous revision")
        juju.refresh(app=APP_NAME, base=CHARM_BASE)

    juju.wait(lambda status: are_agents_idle(status, APP_NAME, idle_period=60))

    if "resume-refresh" in juju.status().apps.get(APP_NAME).app_status.message:
        logger.info("Continue refresh on all other units with `resume-refresh` action")
        resume_unit = leader_unit_name(juju) if substrate == Substrate.K8S else refresh_order[1]
        try:
            juju.run(resume_unit, "resume-refresh")
        except jubilant.TaskError as e:
            if "terminated" in e.task.message:
                logger.info("Unit already terminated before action completed")
            else:
                raise

    # wait for rollback to complete
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        ),
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


def test_upgrade_to_local(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Refresh the charm and upgrade etcd, ensuring high availability while upgrading."""
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
            {"check-compatibility": False, "check-workload-container": False},
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
    resume_unit = leader_unit_name(juju) if substrate == Substrate.K8S else refresh_order[1]
    try:
        juju.run(resume_unit, "resume-refresh")
    except jubilant.TaskError as e:
        if "terminated" in e.task.message:
            logger.info("Unit already terminated before action completed")
        else:
            raise

    # wait for upgrade to complete
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=NUM_UNITS, idle_period=30
        )
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
