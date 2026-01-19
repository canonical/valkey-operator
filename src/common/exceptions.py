# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm-specific exceptions."""


class ValkeyUserManagementError(Exception):
    """Custom Exception if user could not be added or updated in valkey cluster."""
