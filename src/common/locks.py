# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Synchronous locks for cluster operations, used through charmlibs-rollingops."""

import logging
from typing import TYPE_CHECKING, override

from charmlibs.rollingops import SyncLockBackend
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    retry_if_result,
    stop_after_attempt,
    stop_never,
    wait_fixed,
)

from common.client import ValkeyClient
from common.exceptions import ValkeyCannotGetPrimaryIPError, ValkeyWorkloadCommandError
from literals import SCALE_DOWN_LOCK_RETRY_INTERVAL_S, SCALE_DOWN_LOCK_TTL_S, CharmUsers

if TYPE_CHECKING:
    from charm import ValkeyCharm


logger = logging.getLogger(__name__)


class ValkeyScaleDownLockBackend(SyncLockBackend):
    """Scale-down lock stored in Valkey, so that it outlives the unit going away.

    The key expires after the TTL, which clears the lock if the unit never releases it.
    """

    def __init__(self, charm: "ValkeyCharm") -> None:
        self.charm = charm
        self.lock_key = f"scale_down_lock_{charm.app.name}"
        self._acquired = False

    @property
    def client(self) -> ValkeyClient:
        """Get a ValkeyClient instance."""
        return ValkeyClient(
            username=CharmUsers.VALKEY_ADMIN.value,
            password=self.charm.state.unit_server.valkey_admin_password,
            tls=self.charm.state.unit_server.is_tls_enabled,
            workload=self.charm.workload,
        )

    @override
    def acquire(self, timeout: int | None) -> None:
        """Take the lock, retrying every few seconds until the timeout.

        Args:
            timeout: Seconds to keep trying, or None to keep trying indefinitely.

        Raises:
            TimeoutError: If the lock could not be taken within the timeout.
        """
        retrying = Retrying(
            wait=wait_fixed(SCALE_DOWN_LOCK_RETRY_INTERVAL_S),
            stop=(
                stop_never
                if timeout is None
                else stop_after_attempt(max(1, timeout // SCALE_DOWN_LOCK_RETRY_INTERVAL_S))
            ),
            retry=retry_if_result(lambda acquired: not acquired)
            | retry_if_exception_type((ValkeyWorkloadCommandError, ValkeyCannotGetPrimaryIPError)),
            retry_error_callback=lambda _: False,
            after=self._log_failed_attempt,
        )
        if not retrying(self._try_acquire):
            raise TimeoutError(f"Could not take the scale-down lock within {timeout} s")

    def _try_acquire(self) -> bool:
        """Make one attempt, looking the primary up again in case of a failover.

        Returns:
            True once this unit holds the lock or no lock is needed, False if another unit
            holds it.
        """
        unit_name = self.charm.state.unit_server.unit_name
        primary_ip = self.charm.sentinel_manager.get_primary_ip()
        if self.client.get(primary_ip, self.lock_key) == unit_name:
            logger.debug("%s already holds the scale-down lock.", unit_name)
            self._acquired = True
            return True

        if len(self.charm.sentinel_manager.get_active_sentinel_ips(primary_ip)) == 1:
            logger.debug("Last unit in the cluster scaling down, skipping the lock.")
            return True

        self._acquired = self.client.set(
            hostname=primary_ip,
            key=self.lock_key,
            value=unit_name,
            additional_args=["NX", "PX", str(SCALE_DOWN_LOCK_TTL_S * 1000)],
        )
        return self._acquired

    def _log_failed_attempt(self, retry_state: RetryCallState) -> None:
        """Log a failed attempt with its error, if it raised one.

        Args:
            retry_state: The state of the retried call.
        """
        error = retry_state.outcome.exception() if retry_state.outcome else None
        logger.info(
            "%s failed to take the scale-down lock on attempt %d, error: %s",
            self.charm.state.unit_server.unit_name,
            retry_state.attempt_number,
            error,
        )

    @override
    def release(self) -> None:
        """Delete the key if this unit holds it and the scale-down finished.

        If the scale-down failed, Juju retries the hook, so the lock is kept until then or
        until it expires. A failure to delete is only logged, because the TTL clears the key.
        """
        if not self._acquired:
            return

        unit_name = self.charm.state.unit_server.unit_name
        if not self.charm.state.unit_server.is_being_removed:
            logger.warning("Scale-down did not finish, keeping the lock for the retry.")
            return

        try:
            deleted = self.client.delifeq(
                hostname=self.charm.sentinel_manager.get_primary_ip(),
                key=self.lock_key,
                value=unit_name,
            )
        except (ValkeyWorkloadCommandError, ValkeyCannotGetPrimaryIPError):
            logger.warning("Failed to release the scale-down lock, it expires with its TTL.")
            return

        if deleted != "1":
            logger.warning("%s did not hold the scale-down lock when releasing it.", unit_name)
