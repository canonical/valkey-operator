#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import subprocess

import jubilant
import pytest

from literals import Substrate
from tests.integration.spaces.helpers import SPACES, subnet

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module", autouse=True)
def spaces(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Create a gateway-less LXD bridge per test space and add the spaces to the model."""
    if substrate != Substrate.VM:
        pytest.skip("Juju spaces are VM-only")

    existing = {
        n["name"]
        for n in json.loads(
            subprocess.check_output(["lxc", "network", "list", "--format", "json"])
        )
    }
    for bridge, address in SPACES.values():
        if bridge in existing:
            continue
        logger.info("Creating LXD bridge %s on %s", bridge, address)
        subprocess.run(
            ["lxc", "network", "create", bridge, "--type=bridge", f"ipv4.address={address}"]
            + ["ipv4.nat=true", "ipv6.address=none", "dns.mode=none"],
            check=True,
        )
        # no default route or DNS server, so each bridge only reaches its own subnet
        subprocess.run(
            ["lxc", "network", "set", bridge, "raw.dnsmasq", "dhcp-option=3\ndhcp-option=6"],
            check=True,
        )

    # every move must converge without update-status
    juju.model_config({"update-status-hook-interval": "60m"})
    juju.cli("reload-spaces")
    added = {s["name"] for s in json.loads(juju.cli("spaces", "--format", "json"))["spaces"]}
    for space in SPACES.keys() - added:
        juju.cli("add-space", space, subnet(space))
