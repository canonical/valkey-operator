# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm-specific exceptions."""


class ValkeyClientError(Exception):
    """Custom Exception if user could not be added or updated in Valkey cluster."""


class ValkeyCustomCommandError(ValkeyClientError):
    """Custom Exception if a custom command fails on Valkey cluster."""


class ValkeyACLLoadError(ValkeyClientError):
    """Custom Exception if ACL file could not be loaded in Valkey cluster."""


class ValkeyTLSLoadError(ValkeyClientError):
    """Custom Exception if TLS settings could not be loaded in Valkey."""


class ValkeyWorkloadCommandError(Exception):
    """Custom Exception if any workload-related command fails."""
