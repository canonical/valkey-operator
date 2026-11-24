#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from platform import machine

import jubilant
from tenacity import Retrying, stop_after_delay, wait_fixed

from literals import PEER_RELATION
from statuses import ClusterStatuses
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
    start_continuous_writes,
    stop_continuous_writes,
)
from tests.integration.helpers import (
    APP_NAME,
    DEPLOY_TIMEOUT_S,
    GLIDE_RUNNER_NAME,
    CharmUsers,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    get_data_bag,
    get_password,
)
from tests.integration.spaces.helpers import NUM_UNITS, assert_in_space
from tests.integration.upgrades.literals import CHARM_CHANNEL, CHARM_REVISIONS_TO_DEPLOY

logger = logging.getLogger(__name__)


def recorded_addresses(juju: jubilant.Juju) -> dict[str, str]:
    """Map each unit to the address it records in the peer databag."""
    peers = get_data_bag(juju, APP_NAME, PEER_RELATION)
    return {unit: peers[unit].get("private-ip") for unit in juju.status().get_units(APP_NAME)}


def unit_messages(status: jubilant.Status, *units: str) -> set[str]:
    """Return the workload status messages of `units`."""
    return {status.get_units(APP_NAME)[unit].workload_status.message for unit in units}


def test_deploy(juju: jubilant.Juju, glide_runner_charm: str) -> None:
    """Deploy the stable revision with only valkey-peers bound to `data`."""
    juju.deploy(
        APP_NAME,
        num_units=NUM_UNITS,
        channel=CHARM_CHANNEL,
        revision=CHARM_REVISIONS_TO_DEPLOY[machine()],
        constraints={"spaces": "alpha,data,alt"},
        bind="alpha valkey-peers=data",
    )
    juju.deploy(
        glide_runner_charm,
        app=GLIDE_RUNNER_NAME,
        constraints={"spaces": "alpha,data,alt"},
        bind="alpha valkey-client=data",
    )
    juju.integrate(f"{APP_NAME}:valkey-client", f"{GLIDE_RUNNER_NAME}:valkey-client")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, GLIDE_RUNNER_NAME, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )


def test_rebind_during_refresh(juju: jubilant.Juju, charm: str) -> None:
    """Refresh keeps every address, a bind during the pause moves units only after the refresh."""
    addresses = recorded_addresses(juju)
    start_continuous_writes(juju, clear=True)

    # refresh always happens from highest to lowest unit number
    refresh_order = sorted(
        juju.status().get_units(APP_NAME), key=lambda unit: int(unit.split("/")[1]), reverse=True
    )
    juju.refresh(APP_NAME, path=charm)
    # a local charm is always incompatible with the store revision
    juju.wait(lambda status: "incompatible" in status.apps[APP_NAME].app_status.message)
    juju.run(
        refresh_order[0],
        "force-refresh-start",
        {"check-compatibility": False, "check-workload-container": False},
    )
    juju.wait(
        lambda status: (
            "resume-refresh" in status.apps[APP_NAME].app_status.message
            and unit_messages(status, refresh_order[0])
            == {ClusterStatuses.CLIENT_SPACE_MISMATCH.value.message}
        ),
        timeout=900,
    )
    assert recorded_addresses(juju) == addresses

    juju.cli("bind", APP_NAME, "valkey-peers=alt", "valkey-client=alt")
    juju.wait(
        lambda status: (
            unit_messages(status, *refresh_order) == {ClusterStatuses.MOVING_ADDRESS.value.message}
            and are_agents_idle(status, APP_NAME, idle_period=30)
        ),
        timeout=600,
    )
    assert recorded_addresses(juju) == addresses, "a unit moved before resume-refresh"

    juju.run(refresh_order[1], "resume-refresh")
    for attempt in Retrying(stop=stop_after_delay(1200), wait=wait_fixed(10), reraise=True):
        with attempt:
            moved = assert_in_space(juju, "alt", requirer=GLIDE_RUNNER_NAME)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    assert_continuous_writes_increasing(juju)
    stats = stop_continuous_writes(juju)
    # the refresh failover of a leader primary lags, so the outage is logged, not asserted
    logger.info("Longest client outage: %.1f s", stats.max_outage_s)
    assert stats.total_count == stats.last_written_value + 1, stats
    assert_continuous_writes_consistent(
        endpoints=moved,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju),
        last_written_value=stats.last_written_value,
    )
