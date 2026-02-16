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
    SERVICE_NOT_STARTED = StatusObject(
        status="blocked",
        message="Service not started",
    )
    SECRET_ACCESS_ERROR = StatusObject(
        status="blocked",
        message="Cannot access configured secret, check permissions",
        running="async",
    )
    WAITING_TO_START = StatusObject(
        status="maintenance",
        message="Waiting for leader to allow service start",
    )
    CONFIGURATION_ERROR = StatusObject(
        status="blocked",
        message="Configuration error, check logs for details",
        running="async",
    )


class ClusterStatuses(Enum):
    """Collection of possible cluster related statuses."""

    PASSWORD_UPDATE_FAILED = StatusObject(
        status="blocked",
        message="Failed to update an internal user's password",
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
        message="Waiting for the primary unit to start...",
    )


class ValkeyServiceStatuses(Enum):
    """Collection of possible Valkey service related statuses."""

    SERVICE_STARTING = StatusObject(
        status="maintenance",
        message="Waiting for Valkey to start...",
        running="async",
    )
    SERVICE_NOT_RUNNING = StatusObject(
        status="blocked",
        message="Valkey service not running",
        running="async",
    )
