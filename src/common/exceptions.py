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


class SentinelIncorrectReplicaCountError(Exception):
    """Custom Exception if the sentinel sees an incorrect number of replicas."""


class RequestingLockTimedOutError(Exception):
    """Custom Exception if requesting a lock times out."""


class ValkeyCertificatesNotReadyError(Exception):
    """Custom Exception if not all units have stored the TLS certificates."""


class KubernetesClientError(Exception):
    """Custom Exception if a connection to the Kubernetes Cluster API fails."""


class ValkeyBackupError(Exception):
    """Raised when a backup operation fails."""
