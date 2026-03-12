# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Helper functions for the charm."""

import ipaddress
import socket


def is_valid_ip(ip_string: str) -> bool:
    """Check if the provided string is a valid IP address.

    Args:
        ip_string: The string to check.

    Returns:
        bool: True if the string is a valid IP address, False otherwise.
    """
    try:
        ipaddress.ip_address(ip_string)
        return True
    except ValueError:
        return False


def dotappend(string: str) -> str:
    """Append a dot to a string if it does not already end with one."""
    if not string.endswith("."):
        string += "."
    return string


def get_k8s_fqdn(name: str) -> str:
    """Resolve the canonical FQDN for a Kubernetes service or pod name."""
    try:
        info = socket.getaddrinfo(
            name,
            None,
            family=socket.AF_UNSPEC,
            flags=socket.AI_CANONNAME,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as e:
        raise RuntimeError(f"Failed to resolve canonical name for {name}") from e

    for entry in info:
        if canonname := entry[3]:
            return canonname

    raise RuntimeError(f"Could not determine canonical name for {name}")
