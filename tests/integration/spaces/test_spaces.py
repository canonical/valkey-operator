#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import jubilant
import pytest
from tenacity import Retrying, stop_after_delay, wait_fixed

from literals import CLIENT_PORT
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
    are_apps_active_and_agents_idle,
    does_status_match,
    get_password,
)
from tests.integration.spaces.helpers import (
    MAX_OUTAGE_S,
    NUM_UNITS,
    assert_in_space,
    published_hosts,
    sentinel_primary,
)

logger = logging.getLogger(__name__)

ISOLATED_RUNNER_NAME = "glide-runner-iso"


def test_deploy(juju: jubilant.Juju, charm: str, glide_runner_charm: str) -> None:
    """Deploy with the data endpoints bound to `data` and check nothing serves on `alpha`."""
    juju.deploy(
        charm,
        num_units=NUM_UNITS,
        constraints={"spaces": "alpha,data,alt"},
        bind="alpha valkey-peers=data valkey-client=data",
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

    assert_in_space(juju, "data", requirer=GLIDE_RUNNER_NAME)


def test_writes_through_relation(juju: jubilant.Juju) -> None:
    """Write through the endpoints the client relation publishes."""
    start_continuous_writes(juju, clear=True)
    assert_continuous_writes_increasing(juju)
    stats = stop_continuous_writes(juju)
    assert_continuous_writes_consistent(
        endpoints=assert_in_space(juju, "data"),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju),
        last_written_value=stats.last_written_value,
    )


def test_isolated_client(juju: jubilant.Juju, glide_runner_charm: str) -> None:
    """A client with no route to `data` cannot reach any endpoint Valkey publishes."""
    juju.deploy(
        glide_runner_charm,
        app=ISOLATED_RUNNER_NAME,
        constraints={"spaces": "alpha,client-iso"},
        bind="alpha valkey-client=client-iso",
    )
    juju.integrate(f"{APP_NAME}:valkey-client", f"{ISOLATED_RUNNER_NAME}:valkey-client")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, ISOLATED_RUNNER_NAME, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )

    unit = next(iter(juju.status().get_units(ISOLATED_RUNNER_NAME)))
    # the LXD host routes between bridges, drop the default route so only the NICs' subnets work
    juju.exec("ip route flush default", unit=unit)
    for host in published_hosts(juju, ISOLATED_RUNNER_NAME):
        with pytest.raises(jubilant.TaskError):
            juju.exec(f"timeout 5 bash -c '</dev/tcp/{host}/{CLIENT_PORT}'", unit=unit)

    juju.remove_application(ISOLATED_RUNNER_NAME)
    juju.wait(lambda status: ISOLATED_RUNNER_NAME not in status.apps)


def test_client_space_mismatch(juju: jubilant.Juju) -> None:
    """Binding valkey-client away from valkey-peers blocks, binding it back clears it."""
    juju.cli("bind", APP_NAME, "valkey-client=alt")
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [ClusterStatuses.CLIENT_SPACE_MISMATCH.value]},
        ),
        timeout=600,
    )

    juju.cli("bind", APP_NAME, "valkey-client=data")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )
    assert_in_space(juju, "data", requirer=GLIDE_RUNNER_NAME)


def test_no_address_in_space(juju: jubilant.Juju) -> None:
    """Binding to a space the machines have no NIC in blocks, binding back clears it."""
    juju.cli("bind", "--force", APP_NAME, "valkey-peers=client-iso", "valkey-client=client-iso")
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_unit_statuses={APP_NAME: [ClusterStatuses.NO_ADDRESS_IN_SPACE.value]},
        ),
        timeout=600,
    )

    juju.cli("bind", APP_NAME, "valkey-peers=data", "valkey-client=data")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )
    assert_in_space(juju, "data", requirer=GLIDE_RUNNER_NAME)


def test_rebind_under_writes(juju: jubilant.Juju) -> None:
    """Move every unit to `alt` with `juju bind` and lose no write."""
    addresses = assert_in_space(juju, "data")
    epoch = int(sentinel_primary(juju, addresses[0], tls_enabled=False)["config-epoch"])
    start_continuous_writes(juju, clear=True)

    juju.cli("bind", APP_NAME, "valkey-peers=alt", "valkey-client=alt")
    for attempt in Retrying(stop=stop_after_delay(900), wait=wait_fixed(10), reraise=True):
        with attempt:
            addresses = assert_in_space(juju, "alt", requirer=GLIDE_RUNNER_NAME)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    # one failover is expected, an early primary move can cost a second one (spec open issue 2)
    moved_epoch = int(sentinel_primary(juju, addresses[0], tls_enabled=False)["config-epoch"])
    logger.info("Failovers during the rebind: %d", moved_epoch - epoch)

    assert_continuous_writes_increasing(juju)
    stats = stop_continuous_writes(juju)
    # every acknowledged write survived, and no failover kept clients out for too long
    assert stats.total_count == stats.last_written_value + 1, stats
    assert stats.max_outage_s <= MAX_OUTAGE_S, stats
    assert_continuous_writes_consistent(
        endpoints=addresses,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju),
        last_written_value=stats.last_written_value,
    )
