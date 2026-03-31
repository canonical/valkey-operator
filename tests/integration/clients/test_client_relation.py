#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
import pytest

from literals import Substrate
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"
REQUIRER_V1_NAME = "req-v1"
REQUIRER_V0_NAME = "req-v0"


@pytest.fixture
def requirer_charm(arch: str) -> str:
    """Path to the requirer charm file to use for testing."""
    return f"./tests/integration/clients/requirer-charm/requirer-charm_ubuntu@24.04-{arch}.charm"


def test_build_and_deploy(
    charm: str, juju: jubilant.Juju, substrate: Substrate, requirer_charm: str
) -> None:
    """Deploy the charm under test, the client application charms and a TLS provider."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.deploy(
        requirer_charm,
        app=REQUIRER_V0_NAME,
        config={"data-interfaces-version": "0"},
    )
    juju.deploy(
        requirer_charm,
        app=REQUIRER_V1_NAME,
        config={"data-interfaces-version": "1"},
    )
    juju.deploy(TLS_NAME, channel=TLS_CHANNEL)

    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )


def test_integrate_client_interface_v0(juju: jubilant.Juju) -> None:
    """Create the client integration."""
    logger.info("Integrating client applications")
    juju.integrate(APP_NAME, REQUIRER_V0_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )

    requirer_unit = next(iter(juju.status().get_units(REQUIRER_V0_NAME)))
    logger.info("Trying to access global keyspace - should be denied")
    get_credentials_action = juju.run(requirer_unit, "get-credentials")
    username = get_credentials_action.results["usernames"]

    with pytest.raises(jubilant.TaskError) as task_error:
        juju.run(
            requirer_unit, "set", params={"key": TEST_KEY, "value": TEST_VALUE, "user": username}
        )
    assert "NOPERM" in str(task_error), "Action expected to fail because of permission denied"

    logger.info("Trying to access granted keyspace")
    set_action = juju.run(
        requirer_unit,
        "set",
        params={"key": f"requirer-charm:{TEST_KEY}", "value": TEST_VALUE, "user": username},
    )
    assert set_action.status == "completed", "Action should succeed"

    get_action = juju.run(
        requirer_unit, "get", params={"key": f"requirer-charm:{TEST_KEY}", "user": username}
    )
    assert get_action.status == "completed", "Action should succeed"
    result = get_action.results["result"]
    assert result == TEST_VALUE


def test_integrate_client_interface_v1(juju: jubilant.Juju) -> None:
    """Create the client integration."""
    logger.info("Integrating client applications")
    juju.integrate(APP_NAME, REQUIRER_V1_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )

    requirer_unit = next(iter(juju.status().get_units(REQUIRER_V1_NAME)))
    logger.info("Trying to access global keyspace - should be denied")
    get_credentials_action = juju.run(requirer_unit, "get-credentials")
    usernames = get_credentials_action.results["usernames"]
    user_restricted_keyspace, user_global_keyspace = usernames.split(",")

    with pytest.raises(jubilant.TaskError) as task_error:
        juju.run(
            requirer_unit,
            "set",
            params={"key": TEST_KEY, "value": TEST_VALUE, "user": user_restricted_keyspace},
        )
    assert "NOPERM" in str(task_error), "Action expected to fail because of permission denied"

    logger.info("Trying to access granted keyspace with restricted permissions")
    set_action = juju.run(
        requirer_unit,
        "set",
        params={
            "key": f"requirer-charm:{TEST_KEY}",
            "value": TEST_VALUE,
            "user": user_restricted_keyspace,
        },
    )
    assert set_action.status == "completed", "Action should succeed"

    get_action = juju.run(
        requirer_unit,
        "get",
        params={"key": f"requirer-charm:{TEST_KEY}", "user": user_restricted_keyspace},
    )
    assert get_action.status == "completed", "Action should succeed"
    result = get_action.results["result"]
    assert result == TEST_VALUE

    logger.info("Trying to access global keyspace with unrestricted permissions")
    set_action = juju.run(
        requirer_unit,
        "set",
        params={"key": TEST_KEY, "value": TEST_VALUE, "user": user_global_keyspace},
    )
    assert set_action.status == "completed", "Action should succeed"

    get_action = juju.run(
        requirer_unit, "get", params={"key": TEST_KEY, "user": user_global_keyspace}
    )
    assert get_action.status == "completed", "Action should succeed"
    result = get_action.results["result"]
    assert result == TEST_VALUE


def test_enable_tls(juju: jubilant.Juju) -> None:
    """Enable TLS on Valkey and the clients and ensure they can still read and write."""
    logger.info("Enabling client TLS")
    juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.integrate(f"{REQUIRER_V0_NAME}:certificates", TLS_NAME)
    juju.integrate(f"{REQUIRER_V1_NAME}:certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30, unit_count=NUM_UNITS),
        timeout=600,
    )

    logger.info("Ensure TLS access for v0 client")
    requirer_unit = next(iter(juju.status().get_units(REQUIRER_V0_NAME)))
    get_credentials_action = juju.run(requirer_unit, "get-credentials")
    username = get_credentials_action.results["usernames"]

    set_action = juju.run(
        requirer_unit,
        "set",
        params={"key": f"requirer-charm:{TEST_KEY}", "value": TEST_VALUE, "user": username},
    )
    assert set_action.status == "completed", "Action should succeed"

    get_action = juju.run(
        requirer_unit,
        "get",
        params={"key": f"requirer-charm:{TEST_KEY}", "user": username},
    )
    assert get_action.status == "completed", "Action should succeed"
    result = get_action.results["result"]
    assert result == TEST_VALUE

    logger.info("Ensure TLS access for v1 client")
    requirer_unit = next(iter(juju.status().get_units(REQUIRER_V1_NAME)))
    get_credentials_action = juju.run(requirer_unit, "get-credentials")
    usernames = get_credentials_action.results["usernames"]
    user_restricted_keyspace, user_global_keyspace = usernames.split(",")

    set_action = juju.run(
        requirer_unit,
        "set",
        params={
            "key": f"requirer-charm:{TEST_KEY}",
            "value": TEST_VALUE,
            "user": user_restricted_keyspace,
        },
    )
    assert set_action.status == "completed", "Action should succeed"

    get_action = juju.run(
        requirer_unit,
        "get",
        params={"key": f"requirer-charm:{TEST_KEY}", "user": user_restricted_keyspace},
    )
    assert get_action.status == "completed", "Action should succeed"
    result = get_action.results["result"]
    assert result == TEST_VALUE

    set_action = juju.run(
        requirer_unit,
        "set",
        params={"key": TEST_KEY, "value": TEST_VALUE, "user": user_global_keyspace},
    )
    assert set_action.status == "completed", "Action should succeed"

    get_action = juju.run(
        requirer_unit,
        "get",
        params={"key": TEST_KEY, "user": user_global_keyspace},
    )
    assert get_action.status == "completed", "Action should succeed"
    result = get_action.results["result"]
    assert result == TEST_VALUE
