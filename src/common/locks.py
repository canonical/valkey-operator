# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of lock names for cluster operations."""

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING, Protocol, override

from common.client import ValkeyClient
from core.cluster_state import ClusterState
from literals import CharmUsers

if TYPE_CHECKING:
    from charm import ValkeyCharm
    from core.cluster_state import ClusterState
    from core.models import ValkeyServer


logger = logging.getLogger(__name__)


class Lockable(Protocol):
    """Protocol for lockable operations."""

    @property
    def name(self) -> str:
        """Get the name of the lock."""
        return self.__class__.__name__.lower()

    @abstractmethod
    def request_lock(self) -> None:
        """Request the lock for the local unit."""
        raise NotImplementedError

    @abstractmethod
    def release_lock(self) -> None:
        """Release the lock from the local unit."""
        raise NotImplementedError

    @property
    @abstractmethod
    def do_i_hold_lock(self) -> bool:
        """Check if the local unit holds the lock."""
        raise NotImplementedError


class DataBagLock(Lockable):
    """Base class for locks."""

    unit_request_lock_atr_name: str
    member_with_lock_atr_name: str

    def __init__(self, state: "ClusterState") -> None:
        self.state = state

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
        raise NotImplementedError

    @property
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


class StartLock(DataBagLock):
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


class ScaleDownLock(Lockable):
    """Lock for scale down operations.

    This will use valkey to store the lock state and will check if the unit with the lock has completed its scale down operation
    """

    lock_key = "scale_down_lock"

    def __init__(self, charm: "ValkeyCharm") -> None:
        self.charm = charm

    @property
    def client(self) -> ValkeyClient:
        """Get a ValkeyClient instance."""
        return ValkeyClient(
            username=CharmUsers.VALKEY_ADMIN.value,
            password=self.charm.state.unit_server.valkey_admin_password,
            workload=self.charm.workload,
        )

    @property
    def unit_with_lock(self) -> str | None:
        """Get the unit that currently holds the start lock."""
        return self.client.get_value(self.charm.sentinel_manager.get_primary_ip(), self.lock_key)

    @override
    def request_lock(self) -> None:
        """Request the lock for the local unit."""
        if not self.unit_with_lock:
            self.client.set_value(
                hostname=self.charm.sentinel_manager.get_primary_ip(),
                key=self.lock_key,
                value=self.charm.state.unit_server.unit_name,
            )
            logger.info(f"{self.charm.state.unit_server.unit_name} requested {self.name} lock.")
        else:
            logger.info(
                f"{self.charm.state.unit_server.unit_name} attempted to request {self.name} lock, but it is currently held by {self.unit_with_lock}."
            )

    @property
    def do_i_hold_lock(self) -> bool:
        """Check if the local unit holds the lock."""
        return (
            self.unit_with_lock is not None
            and self.unit_with_lock == self.charm.state.unit_server.unit_name
        )

    def release_lock(self) -> None:
        """Release the lock from the local unit."""
        if self.do_i_hold_lock:
            self.client.set_value(
                hostname=self.charm.sentinel_manager.get_primary_ip(),
                key=self.lock_key,
                value="",
            )
            logger.info(f"{self.charm.state.unit_server.unit_name} released {self.name} lock.")
        else:
            logger.info(
                f"{self.charm.state.unit_server.unit_name} attempted to release {self.name} lock, but it is currently held by {self.unit_with_lock if self.unit_with_lock else 'no one'}."
            )

    @property
    def is_lock_free_to_give(self) -> bool:
        """Check if the unit with the lock has completed its operation."""
        return not self.unit_with_lock
