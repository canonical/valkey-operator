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


class ValkeyCannotGetPrimaryIPError(ValkeyClientError):
    """Custom Exception if the primary IP cannot be determined from the sentinels."""


class ValkeyWorkloadCommandError(Exception):
    """Custom Exception if any workload-related command fails."""


class ValkeyServicesFailedToStartError(Exception):
    """Custom Exception if Valkey service fails to start."""


class ValkeyServiceNotAliveError(Exception):
    """Custom Exception if Valkey service is not alive after start."""


class ValkeyConfigurationError(Exception):
    """Custom Exception if Valkey configuration fails to be set."""


class SentinelFailoverError(Exception):
    """Custom Exception if triggering sentinel failover fails."""


class ValkeyServicesCouldNotBeStoppedError(Exception):
    """Custom Exception if Valkey services could not be stopped."""


class CannotSeeAllActiveSentinelsError(Exception):
    """Custom Exception if the local sentinel cannot see all active sentinels in the cluster."""
