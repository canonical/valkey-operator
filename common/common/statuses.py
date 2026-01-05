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