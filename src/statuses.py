# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Statuses for the Charmed Valkey Operator.

This module defines various status enums that represent the state of the charm,
"""

from enum import Enum

from data_platform_helpers.advanced_statuses.models import StatusObject


class CharmStatuses(Enum):
    """Collection of possible statuses for the charm."""

    ACTIVE_IDLE = StatusObject(status="active", message="")
    SERVICE_NOT_STARTED = StatusObject(status="blocked", message="Service not started")
    SECRET_ACCESS_ERROR = StatusObject(
        status="blocked",
        message="Cannot access configured secret, check permissions",
        running="async",
    )


class ClusterStatuses(Enum):
    """Collection of possible cluster related statuses."""

    PASSWORD_UPDATE_FAILED = StatusObject(
        status="blocked", message="Failed to update an internal user's password", running="async"
    )

    WAITING_FOR_SENTINEL_DISCOVERY = StatusObject(
        status="maintenance",
        message="Waiting for sentinel to be discovered by other units...",
        running="async",
    )

    WAITING_FOR_REPLICA_SYNC = StatusObject(
        status="maintenance",
        message="Waiting for replica to sync with primary...",
        running="async",
    )


class ValkeyServiceStatuses(Enum):
    """Collection of possible Valkey service related statuses."""

    SERVICE_STARTING = StatusObject(
        status="maintenance", message="waiting for valkey to start...", running="async"
    )
    SERVICE_NOT_RUNNING = StatusObject(
        status="blocked", message="valkey service not running", running="async"
    )
