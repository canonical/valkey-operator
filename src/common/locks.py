# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of lock names for cluster operations."""

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.cluster_state import ClusterState
    from core.models import ValkeyServer


logger = logging.getLogger(__name__)


class Lock(ABC):
    """Base class for locks."""

    unit_request_lock_atr_name: str
    member_with_lock_atr_name: str

    def __init__(self, state: "ClusterState") -> None:
        self.state = state

    @property
    def name(self) -> str:
        """Get the name of the lock."""
        return self.__class__.__name__.lower()

    @property
    def units_requesting_lock(self) -> list[str]:
        """Get the list of units requesting the start lock."""
        return [
            unit.unit_name
            for unit in self.state.servers
            if unit.model and getattr(unit.model, self.unit_request_lock_atr_name, False)
        ]

    @property
    def next_unit_to_give_lock(self) -> str | None:
        """Get the next unit to give the start lock to."""
        return self.units_requesting_lock[0] if self.units_requesting_lock else None

    @property
    def unit_with_lock(self) -> "ValkeyServer | None":
        """Get the unit that currently holds the start lock."""
        return next(
            (
                unit
                for unit in self.state.servers
                if unit.unit_name
                == getattr(self.state.cluster.model, self.member_with_lock_atr_name, "")
            ),
            None,
        )

    @property
    @abstractmethod
    def is_lock_free_to_give(self) -> bool:
        """Check if the unit with the lock has completed its operation."""
        pass

    def do_i_hold_lock(self) -> bool:
        """Check if the local unit holds the start lock."""
        return self.state.unit_server.unit_name == getattr(
            self.state.cluster.model, self.member_with_lock_atr_name, ""
        )

    def request_lock(self) -> None:
        """Request the lock for the local unit."""
        self.state.unit_server.update(
            {
                self.unit_request_lock_atr_name: True,
            }
        )
        if self.state.unit_server.unit.is_leader():
            logger.info(
                f"Leader unit requesting {self.name} lock. Triggering lock request processing."
            )
            self.process()

    def release_lock(self) -> None:
        """Release the lock from the local unit."""
        self.state.unit_server.update(
            {
                self.unit_request_lock_atr_name: False,
            }
        )
        if self.state.unit_server.unit.is_leader():
            logger.info(
                f"Leader unit releasing {self.name} lock. Triggering lock request processing."
            )
            self.process()

    def process(self) -> None:
        """Process the lock requests and update the unit with the lock."""
        if not self.state.unit_server.unit.is_leader():
            logger.info(f"Only the leader can process {self.name} lock requests.")
            return

        if self.is_lock_free_to_give:
            next_unit = self.next_unit_to_give_lock
            self.state.cluster.update({self.member_with_lock_atr_name: next_unit})
            logger.debug(f"Gave {self.name} lock to {next_unit}")
        logger.debug(
            f"{self.name} lock is currently held by {getattr(self.state.cluster.model, self.member_with_lock_atr_name)}"
        )


class StartLock(Lock):
    """Lock for starting operations."""

    unit_request_lock_atr_name = "request_start_lock"
    member_with_lock_atr_name = "start_member"

    @property
    def is_lock_free_to_give(self) -> bool:
        """Check if the unit with the start lock has completed its operation."""
        starting_unit = self.unit_with_lock
        return (
            not self.state.cluster.model.start_member
            or not starting_unit
            or starting_unit.is_started
        )


class ScaleDownLock(Lock):
    """Lock for scale down operations."""

    unit_request_lock_atr_name = "request_scale_down_lock"
    member_with_lock_atr_name = "scale_down_member"

    @property
    def is_lock_free_to_give(self) -> bool:
        """Check if the unit with the scale down lock has completed its operation."""
        scaling_down_unit = self.unit_with_lock
        return (
            not self.state.cluster.model.scale_down_member
            or not scaling_down_unit
            or scaling_down_unit.model.request_scale_down_lock is False
        )
