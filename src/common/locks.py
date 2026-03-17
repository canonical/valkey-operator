# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of locks for cluster operations."""

import logging
import time
from abc import abstractmethod
from typing import TYPE_CHECKING, Protocol, override

from tenacity import Retrying, stop_after_attempt, wait_fixed

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
    def request_lock(self) -> bool:
        """Request the lock for the local unit."""
        raise NotImplementedError

    @abstractmethod
    def release_lock(self) -> bool:
        """Release the lock from the local unit."""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_held_by_this_unit(self) -> bool:
        """Check if the local unit holds the lock."""
        raise NotImplementedError


class DataBagLock(Lockable):
    """Base class for locks."""

    unit_request_lock_atr_name: str
    member_with_lock_atr_name: str
    lock_timestamp: str = "databaglock_timestamp"

    def __init__(self, state: "ClusterState") -> None:
        self.state = state

    def __init_subclass__(cls) -> None:
        """Initialize subclass attributes."""
        super().__init_subclass__()
        cls.lock_timestamp = cls.__name__.lower() + "_timestamp"

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
        if self.state.unit_server.model[self.unit_request_lock_atr_name]:
            return self.state.unit_server.unit_name
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
    def is_held_by_this_unit(self) -> bool:
        """Check if the local unit holds the start lock."""
        return self.state.unit_server.unit_name == getattr(
            self.state.cluster.model, self.member_with_lock_atr_name, ""
        )

    def request_lock(self) -> bool:
        """Request the lock for the local unit."""
        if not self.state.unit_server.model[self.unit_request_lock_atr_name]:
            self.state.unit_server.update(
                {
                    self.unit_request_lock_atr_name: True,
                    self.lock_timestamp: time.time(),
                }
            )
        if self.state.unit_server.unit.is_leader():
            logger.info(
                "Leader unit requesting %s lock. Triggering lock request processing.",
                self.name,
            )
            self.process()

        return self.is_held_by_this_unit

    def release_lock(self) -> bool:
        """Release the lock from the local unit."""
        if self.state.unit_server.model[self.unit_request_lock_atr_name]:
            self.state.unit_server.update(
                {
                    self.unit_request_lock_atr_name: False,
                    self.lock_timestamp: time.time(),
                }
            )
        if self.state.unit_server.unit.is_leader():
            logger.info(
                "Leader unit releasing %s lock. Triggering lock request processing.",
                self.name,
            )
            self.process()

        return True

    def process(self) -> None:
        """Process the lock requests and update the unit with the lock."""
        if not self.state.unit_server.unit.is_leader():
            logger.info("Only the leader can process lock requests.")
            return

        if self.is_lock_free_to_give:
            next_unit = self.next_unit_to_give_lock
            self.state.cluster.update({self.member_with_lock_atr_name: next_unit})
            logger.debug("Gave %s to %s", self.name, next_unit)

        if unit_with_lock := self.state.cluster.model[self.member_with_lock_atr_name]:
            logger.debug("%s is currently held by %s", self.name, unit_with_lock)


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
            or not starting_unit.model.request_start_lock
        )


class ScaleDownLock(Lockable):
    """Lock for scale down operations.

    This will use valkey to store the lock state and will check if the unit with the lock has completed its scale down operation
    """

    def __init__(self, charm: "ValkeyCharm") -> None:
        self.charm = charm
        self.lock_key = f"scale_down_lock_{self.charm.app.name}"

    @property
    def client(self) -> ValkeyClient:
        """Get a ValkeyClient instance."""
        return ValkeyClient(
            username=CharmUsers.VALKEY_ADMIN.value,
            password=self.charm.state.unit_server.valkey_admin_password,
            tls=self.charm.state.unit_server.is_tls_enabled,
            workload=self.charm.workload,
        )

    def get_unit_with_lock(self, primary_ip: str | None = None) -> str | None:
        """Get the unit that currently holds the start lock."""
        return self.client.get(
            primary_ip or self.charm.sentinel_manager.get_primary_ip(), self.lock_key
        )

    @override
    def request_lock(self, timeout: int | None = None, primary_ip: str | None = None) -> bool:
        """Request the lock for the local unit.

        This method will keep trying to acquire the lock until it is acquired or until the timeout is reached (if provided).

        Args:
            timeout (int | None): The maximum time to keep trying to acquire the lock, in seconds. If None, it will keep trying indefinitely.
            primary_ip (str | None): The primary IP to use for the lock. If None, it will get the current primary IP from the sentinel manager.

        Returns:
            bool: True if the lock was acquired, False if the timeout was reached before acquiring the lock.
        """
        logger.debug(
            "%s is requesting %s lock.", self.charm.state.unit_server.unit_name, self.name
        )
        primary_ip = primary_ip or self.charm.sentinel_manager.get_primary_ip()
        if self.get_unit_with_lock(primary_ip) == self.charm.state.unit_server.unit_name:
            logger.debug(
                "%s already holds %s lock. No need to request it again.",
                self.charm.state.unit_server.unit_name,
                self.name,
            )
            return True

        if len(self.charm.sentinel_manager.get_active_sentinel_ips(primary_ip)) == 1:
            logger.debug("Last unit in the cluster scaling down. Lock will be skipped.")
            return True

        number_of_retries = min(timeout // 5 if timeout else 1, 1)

        for attempt in Retrying(
            wait=wait_fixed(5),
            stop=stop_after_attempt(number_of_retries),
            retry_error_callback=lambda _: False,
            after=lambda retry_state: logger.info(
                "%s failed to acquire %s lock on attempt %d. Retrying in 5 seconds.",
                self.charm.state.unit_server.unit_name,
                self.name,
                retry_state.attempt_number,
            ),
        ):
            with attempt:
                # update the primary ip in case a failover happens when we are waiting to acquire the lock
                primary_ip = self.charm.sentinel_manager.get_primary_ip()
                if self.client.set(
                    hostname=primary_ip,
                    key=self.lock_key,
                    value=self.charm.state.unit_server.unit_name,
                    additional_args=[
                        "NX",
                        "PX",
                        str(
                            5 * 60 * 1000
                        ),  # Set the lock with a TTL of 5 minutes to prevent deadlocks
                    ],
                ):
                    logger.debug(
                        "%s acquired %s lock.", self.charm.state.unit_server.unit_name, self.name
                    )
                    return True

    @property
    def is_held_by_this_unit(self) -> bool:
        """Check if the local unit holds the lock."""
        unit_with_lock = self.get_unit_with_lock()
        return (
            unit_with_lock is not None and unit_with_lock == self.charm.state.unit_server.unit_name
        )

    def release_lock(self, primary_ip: str | None = None) -> bool:
        """Release the lock from the local unit."""
        primary_ip = primary_ip or self.charm.sentinel_manager.get_primary_ip()
        if (
            self.client.delifeq(
                hostname=primary_ip,
                key=self.lock_key,
                value=self.charm.state.unit_server.unit_name,
            )
            == "1"
        ):
            logger.debug("%s released %s lock.", self.charm.state.unit_server.unit_name, self.name)
            return True

        logger.warning(
            "%s failed to release %s lock. It may not have held the lock or it may have already been released.",
            self.charm.state.unit_server.unit_name,
            self.name,
        )
        return False
