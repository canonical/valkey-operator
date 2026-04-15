# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Statuses for the Charmed Valkey Operator.

This module defines various status enums that represent the state of the charm,
"""

from enum import Enum

from data_platform_helpers.advanced_statuses.models import StatusObject


class CharmStatuses(Enum):
    """Collection of possible statuses for the charm."""

    ACTIVE_IDLE = StatusObject(
        status="active",
        message="",
    )
    SECRET_ACCESS_ERROR = StatusObject(
        status="blocked",
        message="Cannot access configured secret, check permissions",
        running="async",
    )


class ClusterStatuses(Enum):
    """Collection of possible cluster related statuses."""

    PASSWORD_UPDATE_FAILED = StatusObject(
        status="blocked",
        message="Failed to update an internal user's password",
        running="async",
    )

    VALKEY_UNHEALTHY_RESTART = StatusObject(
        status="maintenance",
        message="Valkey unhealthy",
    )

    SENTINEL_UNHEALTHY_RESTART = StatusObject(
        status="maintenance",
        message="Sentinel unhealthy",
    )


class StartStatuses(Enum):
    """Collection of possible statuses related to the service start."""

    SERVICE_NOT_STARTED = StatusObject(
        status="maintenance",
        message="Service not started",
    )
    WAITING_TO_START = StatusObject(
        status="maintenance",
        message="Waiting for leader to allow service start",
    )
    CONFIGURATION_ERROR = StatusObject(
        status="blocked",
        message="Configuration error, check logs for details",
    )
    SERVICE_STARTING = StatusObject(
        status="maintenance",
        message="Waiting for Valkey to start...",
        running="async",
    )
    WAITING_FOR_SENTINEL_DISCOVERY = StatusObject(
        status="maintenance",
        message="Waiting for sentinel to start and be discovered by other units...",
    )

    WAITING_FOR_REPLICA_SYNC = StatusObject(
        status="maintenance",
        message="Waiting for replica to sync with primary...",
    )

    WAITING_FOR_PRIMARY_START = StatusObject(
        status="maintenance",
        message="Waiting to discover the primary unit...",
    )
    ERROR_ON_START = StatusObject(
        status="blocked",
        message="Error occurred during service start, check logs for details",
    )


class ScaleDownStatuses(Enum):
    """Collection of possible statuses related to scale down operations."""

    WAIT_FOR_LOCK = StatusObject(
        status="maintenance",
        message="Waiting for lock to scale down...",
        running="async",
    )
    SCALING_DOWN = StatusObject(
        status="maintenance",
        message="Scaling down...",
        running="async",
    )
    GOING_AWAY = StatusObject(
        status="maintenance",
        message="Waiting for juju to remove the unit...",
    )


class TLSStatuses(Enum):
    """Collection of TLS related statuses."""

    ENABLING_CLIENT_TLS = StatusObject(status="maintenance", message="Enabling client TLS...")
    DISABLING_CLIENT_TLS = StatusObject(status="maintenance", message="Disabling client TLS...")
    DISABLING_CLIENT_TLS_FAILED = StatusObject(
        status="blocked", message="Failed to disable client TLS..."
    )
    CERTIFICATE_EXPIRING = StatusObject(
        status="maintenance",
        message="TLS certificates expiring soon. Please ensure new certificates are provided",
        short_message="TLS certificates expiring soon",
    )
    CA_ROTATION_DETECTED = StatusObject(
        status="maintenance", message="TLS CA rotation: new CA detected"
    )
    CA_ROTATION_CA_ADDED = StatusObject(
        status="maintenance", message="TLS CA rotation: new CA certificate added"
    )
    CA_ROTATION_UPDATED = StatusObject(
        status="maintenance", message="TLS CA rotation: certificates updated"
    )
    PRIVATE_KEY_BUT_NO_TLS = StatusObject(
        status="blocked", message="Private Key provided, but client TLS not enabled"
    )
    PRIVATE_KEY_INVALID = StatusObject(
        status="blocked",
        message="The private key provided is not valid. Please provide a valid private key",
    )
    SANS_CONFIG_INVALID = StatusObject(
        status="blocked",
        message="Invalid value for config option 'certificate-extra-sans'",
        short_message="Invalid value `certificate-extra-sans`",
    )
    CERTIFICATE_DENIED = StatusObject(
        status="blocked", message="Certificate request was denied, check logs for details"
    )


class ExternalClientsStatuses(Enum):
    """Collection of external clients related statuses."""

    RESOURCE_REQUEST_UNPROCESSED = StatusObject(
        status="maintenance", message="Client relation: Request not processed yet"
    )

    USER_ACL_OUT_OF_DATE = StatusObject(
        status="maintenance", message="Client relation: Unit has not updated ACLs for client users"
    )
