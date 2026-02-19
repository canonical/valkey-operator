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
    SCALING_NOT_IMPLEMENTED = StatusObject(
        status="blocked",
        message="Scaling Valkey is not implemented yet",
    )
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


class TLSStatuses(Enum):
    """Collection of TLS related statuses."""

    ENABLING_CLIENT_TLS = StatusObject(status="maintenance", message="Enabling client TLS...")
    DISABLING_CLIENT_TLS = StatusObject(status="maintenance", message="Disabling client TLS...")
