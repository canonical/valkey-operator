#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging

import jubilant

from literals import EXTERNAL_CLIENTS_RELATION, PEER_RELATION
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
    get_password,
)
from tests.integration.spaces.helpers import NUM_UNITS, assert_in_space

logger = logging.getLogger(__name__)


def test_deploy_with_default_binding(
    juju: jubilant.Juju, charm: str, glide_runner_charm: str
) -> None:
    """Deploy with only a default binding, so the data endpoints follow it to `data`."""
    juju.deploy(charm, num_units=NUM_UNITS, constraints={"spaces": "alpha,data"}, bind="data")
    juju.deploy(
        glide_runner_charm,
        app=GLIDE_RUNNER_NAME,
        constraints={"spaces": "alpha,data"},
        bind="data",
    )
    juju.integrate(f"{APP_NAME}:valkey-client", f"{GLIDE_RUNNER_NAME}:valkey-client")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, GLIDE_RUNNER_NAME, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )

    bindings = json.loads(juju.cli("show-application", APP_NAME, "--format", "json"))[APP_NAME][
        "endpoint-bindings"
    ]
    assert bindings[PEER_RELATION] == bindings[EXTERNAL_CLIENTS_RELATION] == "data", bindings
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
