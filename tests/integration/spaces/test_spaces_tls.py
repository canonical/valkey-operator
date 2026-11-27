#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import jubilant
from tenacity import Retrying, stop_after_delay, wait_fixed

from literals import PEER_RELATION
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
    start_continuous_writes,
    stop_continuous_writes,
)
from tests.integration.helpers import (
    APP_NAME,
    DEPLOY_TIMEOUT_TLS_S,
    GLIDE_RUNNER_NAME,
    TLS_CHANNEL,
    TLS_NAME,
    CharmUsers,
    are_apps_active_and_agents_idle,
    download_client_certificate_from_unit,
    get_password,
)
from tests.integration.spaces.helpers import (
    MAX_OUTAGE_S,
    NUM_UNITS,
    assert_in_space,
    assert_ip_sans_verify,
    bound_address,
    sentinel_primary,
    served_ip_sans,
)

logger = logging.getLogger(__name__)


def test_deploy_with_tls(juju: jubilant.Juju, charm: str, glide_runner_charm: str) -> None:
    """Deploy on `data` with client TLS from self-signed-certificates."""
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
    juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.integrate(f"{GLIDE_RUNNER_NAME}:certificates", TLS_NAME)
    juju.integrate(f"{APP_NAME}:valkey-client", f"{GLIDE_RUNNER_NAME}:valkey-client")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, GLIDE_RUNNER_NAME, TLS_NAME, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_TLS_S,
    )

    download_client_certificate_from_unit(juju)
    assert_in_space(juju, "data", tls_enabled=True, requirer=GLIDE_RUNNER_NAME)
    assert_ip_sans_verify(juju)


def test_tls_rebind_under_writes(juju: jubilant.Juju) -> None:
    """Move every unit to `alt` while each listening address keeps verifying its IP SANs."""
    addresses = assert_in_space(juju, "data", tls_enabled=True)
    epoch = int(sentinel_primary(juju, addresses[0], tls_enabled=True)["config-epoch"])
    start_continuous_writes(juju, clear=True)

    juju.cli("bind", APP_NAME, "valkey-peers=alt", "valkey-client=alt")
    for attempt in Retrying(stop=stop_after_delay(900), wait=wait_fixed(10), reraise=True):
        # outside `with attempt`, so a SAN failure fails the test instead of being retried
        assert_ip_sans_verify(juju)
        with attempt:
            addresses = assert_in_space(juju, "alt", tls_enabled=True, requirer=GLIDE_RUNNER_NAME)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )
    assert_ip_sans_verify(juju)
    # after the move each unit requests a certificate without its old address
    for attempt in Retrying(stop=stop_after_delay(300), wait=wait_fixed(10), reraise=True):
        with attempt:
            for unit in juju.status().get_units(APP_NAME):
                address = bound_address(juju, unit, PEER_RELATION)
                assert served_ip_sans(address) == {address}, f"{unit}: SANs keep an old address"

    # one failover is expected, an early primary move can cost a second one (spec open issue 2)
    moved_epoch = int(sentinel_primary(juju, addresses[0], tls_enabled=True)["config-epoch"])
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
        tls_enabled=True,
    )
