# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm-specific exceptions."""


class ValkeyClientError(Exception):
    """Custom Exception if user could not be added or updated in valkey cluster."""


class ValkeyCustomCommandError(ValkeyClientError):
    """Custom Exception if a custom command fails on valkey cluster."""


class ValkeyACLLoadError(ValkeyClientError):
    """Custom Exception if ACL file could not be loaded in valkey cluster."""


class ValkeyConfigSetError(ValkeyClientError):
    """Custom Exception if setting configuration on valkey cluster fails."""


class ValkeyExecCommandError(Exception):
    """Custom Exception if exec command on valkey container fails."""
