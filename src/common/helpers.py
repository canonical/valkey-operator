# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


import ipaddress


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
