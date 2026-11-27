#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import re
import subprocess
from ipaddress import ip_address, ip_network

import jubilant

from literals import (
    CLIENT_PORT,
    EXTERNAL_CLIENTS_RELATION,
    PEER_RELATION,
    PRIMARY_NAME,
    SENTINEL_PORT,
    SENTINEL_TLS_PORT,
    TLS_PORT,
    CharmUsers,
)
from tests.integration.helpers import (
    APP_NAME,
    TLS_CA_FILE,
    exec_valkey_cli,
    get_data_bag,
    get_password,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
# space name -> (LXD bridge, bridge address), the bridges push no gateway or DNS
SPACES = {
    "data": ("valkey-data", "10.78.0.1/24"),
    "alt": ("valkey-alt", "10.77.0.1/24"),
    "client-iso": ("valkey-iso", "10.79.0.1/24"),
}
VALKEY_PORTS = {CLIENT_PORT, TLS_PORT, SENTINEL_PORT, SENTINEL_TLS_PORT}
# a leader primary's move cost clients 11.6-12.6 s live on LXD, 29-31 s before 76d68df
MAX_OUTAGE_S = 20


def subnet(space: str) -> str:
    """Return the CIDR of a test space."""
    return str(ip_network(SPACES[space][1], strict=False))


def bound_address(juju: jubilant.Juju, unit: str, endpoint: str) -> str:
    """Return the address Juju gives `endpoint` on `unit`."""
    return juju.exec(f"network-get {endpoint} --bind-address", unit=unit).stdout.strip()


def listening_hosts(juju: jubilant.Juju, unit: str) -> dict[str, set[int]]:
    """Map each address `unit` listens on for Valkey or Sentinel to its ports."""
    hosts: dict[str, set[int]] = {}
    for line in juju.exec("ss -ltnH", unit=unit).stdout.splitlines():
        host, port = line.split()[3].rsplit(":", 1)
        if int(port) in VALKEY_PORTS:
            hosts.setdefault(host, set()).add(int(port))
    return hosts


def published_hosts(juju: jubilant.Juju, requirer: str) -> set[str]:
    """Return every host Valkey publishes to `requirer` over the client relation."""
    unit = next(iter(juju.status().get_units(requirer)))
    info = json.loads(juju.cli("show-unit", unit, "--format", "json"))[unit]
    data = next(r for r in info["relation-info"] if r["endpoint"] == EXTERNAL_CLIENTS_RELATION)[
        "application-data"
    ]
    return {
        endpoint.rsplit(":", 1)[0]
        for request in json.loads(data["requests"])
        for key in ("endpoints", "read-only-endpoints", "sentinel-endpoints")
        for endpoint in (request.get(key) or "").split(",")
        if endpoint
    }


def sentinel_primary(juju: jubilant.Juju, address: str, tls_enabled: bool) -> dict:
    """Return what the Sentinel at `address` knows about the primary."""
    return json.loads(
        exec_valkey_cli(
            address,
            username=CharmUsers.SENTINEL_CHARM_ADMIN.value,
            password=get_password(juju, user=CharmUsers.SENTINEL_CHARM_ADMIN),
            command=f"sentinel master {PRIMARY_NAME}",
            tls_enabled=tls_enabled,
            json=True,
            sentinel=True,
        ).stdout
    )


def assert_in_space(
    juju: jubilant.Juju, space: str, tls_enabled: bool = False, requirer: str | None = None
) -> list[str]:
    """Assert every unit serves Valkey and Sentinel on its address in `space` only.

    Returns:
        The unit addresses in `space`.
    """
    network = ip_network(subnet(space))
    peers = get_data_bag(juju, APP_NAME, PEER_RELATION)
    addresses = []
    for unit in juju.status().get_units(APP_NAME):
        address = bound_address(juju, unit, PEER_RELATION)
        assert ip_address(address) in network, f"{unit}: {PEER_RELATION} on {address}"
        assert bound_address(juju, unit, EXTERNAL_CLIENTS_RELATION) == address
        assert peers[unit].get("private-ip") == address, f"{unit}: databag {peers[unit]}"
        listening = listening_hosts(juju, unit)
        assert listening.keys() == {address}, f"{unit}: listens on {listening}"
        assert {TLS_PORT, SENTINEL_TLS_PORT} <= listening[address], f"{unit}: {listening}"
        announce = exec_valkey_cli(
            address,
            username=CharmUsers.VALKEY_ADMIN.value,
            password=get_password(juju),
            command="config get replica-announce-ip",
            tls_enabled=tls_enabled,
        ).stdout.split()[-1]
        assert announce == address, f"{unit}: replica-announce-ip {announce}"
        addresses.append(address)

    for address in addresses:
        primary = sentinel_primary(juju, address, tls_enabled)
        assert primary["ip"] in addresses, f"Sentinel {address}: primary {primary['ip']}"
        assert int(primary["num-slaves"]) == len(addresses) - 1, f"Sentinel {address}"
        assert int(primary["num-other-sentinels"]) == len(addresses) - 1, f"Sentinel {address}"
        quorum = exec_valkey_cli(
            address,
            username=CharmUsers.SENTINEL_CHARM_ADMIN.value,
            password=get_password(juju, user=CharmUsers.SENTINEL_CHARM_ADMIN),
            command=f"sentinel ckquorum {PRIMARY_NAME}",
            tls_enabled=tls_enabled,
            sentinel=True,
        ).stdout
        assert quorum.startswith("OK"), f"Sentinel {address}: {quorum}"

    if requirer:
        published = published_hosts(juju, requirer)
        assert published and published <= set(addresses), f"published {published}"

    logger.info("Every unit serves on %s in %s", addresses, space)
    return addresses


def served_ip_sans(host: str) -> set[str]:
    """Return the IP SANs of the certificate Valkey serves on `host`."""
    cert = subprocess.run(
        ["openssl", "s_client", "-connect", f"{host}:{TLS_PORT}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    sans = subprocess.run(
        ["openssl", "x509", "-noout", "-ext", "subjectAltName"],
        input=cert,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(re.findall(r"IP Address:([\d.]+)", sans))


def assert_ip_sans_verify(juju: jubilant.Juju) -> None:
    """Assert each address a unit listens on verifies against its certificate's IP SANs."""
    for unit in juju.status().get_units(APP_NAME):
        for host in listening_hosts(juju, unit):
            result = subprocess.run(
                ["openssl", "s_client", "-connect", f"{host}:{TLS_PORT}"]
                + ["-verify_ip", host, "-CAfile", TLS_CA_FILE, "-verify_return_error"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # a restarting unit refuses the connection, that is not a SAN failure
            if "connect:errno" in result.stderr or "Connection refused" in result.stderr:
                continue
            assert "Verify return code: 0 (ok)" in result.stdout, f"{unit} {host}: bad SANs"
