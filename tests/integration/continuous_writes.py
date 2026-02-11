#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import time
from contextlib import contextmanager
from multiprocessing import Event, Process, Queue, log_to_stderr
from types import SimpleNamespace
from typing import Generator

import jubilant
import valkey
from tenacity import (
    RetryError,
    Retrying,
    retry,
    stop_after_attempt,
    stop_after_delay,
    wait_fixed,
    wait_random,
)

from literals import CharmUsers
from tests.integration.helpers import get_cluster_hostnames, get_password

logger = logging.getLogger(__name__)


class WriteFailedError(Exception):
    """Raised when a single write operation has failed."""


class ContinuousWrites:
    """Utility class for managing continuous writes to Valkey."""

    KEY = "cw_key"
    LAST_WRITTEN_VAL_PATH = "last_written_value"
    SENTINEL_PORT = 26379

    def __init__(
        self,
        juju: jubilant.Juju,
        app: str,
        initial_count: int = 0,
        log_written_values: bool = False,
    ):
        self._juju = juju
        self._app = app
        self._is_stopped = True
        self._event = None
        self._queue = None
        self._process = None
        self._initial_count = initial_count
        self._log_written_values = log_written_values

    def _get_config(self) -> SimpleNamespace:
        """Fetch current cluster configuration from Juju."""
        return SimpleNamespace(
            endpoints=",".join(get_cluster_hostnames(self._juju, app_name=self._app)),
            valkey_password=get_password(self._juju, user=CharmUsers.VALKEY_ADMIN),
            sentinel_password=get_password(self._juju, user=CharmUsers.SENTINEL_CHARM_ADMIN),
        )

    @contextmanager
    def _get_client(self) -> Generator[valkey.Valkey, None, None]:
        """Context manager to provide a master client and ensure cleanup."""
        conf = self._get_config()
        sentinel = valkey.Sentinel(
            [(host, self.SENTINEL_PORT) for host in conf.endpoints.split(",")],
            username=CharmUsers.VALKEY_ADMIN.value,
            password=conf.valkey_password,
            sentinel_kwargs={
                "password": conf.sentinel_password,
                "username": CharmUsers.SENTINEL_CHARM_ADMIN.value,
            },
        )
        master = sentinel.master_for("primary")
        try:
            yield master
        finally:
            # Valkey clients use connection pools, but we ensure logical separation
            master.close()

    @retry(wait=wait_fixed(5) + wait_random(0, 5), stop=stop_after_attempt(5))
    def start(self) -> None:
        """Run continuous writes in the background."""
        if not self._is_stopped:
            self.stop()

        self._is_stopped = False
        self._event = Event()
        self._queue = Queue()

        self._process = Process(
            target=self._run_wrapper,
            name="continuous_writes",
            args=(self._event, self._queue, self._initial_count, self._log_written_values),
        )

        self.update()  # Load initial config into queue
        self._process.start()

    def update(self) -> None:
        """Update cluster related conf (scaling, password changes)."""
        if self._queue:
            self._queue.put(self._get_config())

    @retry(wait=wait_fixed(5) + wait_random(0, 5), stop=stop_after_attempt(5))
    def clear(self) -> SimpleNamespace | None:
        """Stop writes and delete the tracking key/file."""
        result = None
        if not self._is_stopped:
            result = self.stop()

        with self._get_client() as client:
            client.delete(self.KEY)

        if os.path.exists(self.LAST_WRITTEN_VAL_PATH):
            os.remove(self.LAST_WRITTEN_VAL_PATH)

        return result

    def count(self) -> int:
        """Return number of items in the list."""
        with self._get_client() as client:
            return client.llen(self.KEY)

    def max_stored_id(self) -> int:
        """Return the most recently inserted ID (top of list)."""
        with self._get_client() as client:
            val = client.lindex(self.KEY, 0)
            return int(val) if val else 0

    @retry(wait=wait_fixed(5) + wait_random(0, 5), stop=stop_after_attempt(5))
    def stop(self) -> SimpleNamespace:
        """Stop the background process and return summary statistics."""
        if not self._is_stopped and self._process:
            self._event.set()
            self._process.join(timeout=30)
            self._process.terminate()
            self._is_stopped = True

        result = SimpleNamespace()
        result.max_stored_id = self.max_stored_id()
        result.count = self.count()

        # Retrieve the last ID the worker attempted to write
        try:
            for attempt in Retrying(stop=stop_after_delay(10), wait=wait_fixed(2)):
                with attempt:
                    with open(self.LAST_WRITTEN_VAL_PATH, "r") as f:
                        result.last_expected_id = int(f.read().strip())
        except (RetryError, FileNotFoundError, ValueError):
            result.last_expected_id = -1

        return result

    @staticmethod
    def _run_wrapper(
        event: Event, data_queue: Queue, starting_number: int, log_written_values: bool = False
    ) -> None:
        """Entry point for the Process; simplified without unnecessary asyncio."""
        proc_logger = log_to_stderr()
        proc_logger.setLevel(logging.INFO)

        def _make_client(conf):
            s = valkey.Sentinel(
                [(h, ContinuousWrites.SENTINEL_PORT) for h in conf.endpoints.split(",")],
                username=CharmUsers.VALKEY_ADMIN.value,
                password=conf.valkey_password,
                sentinel_kwargs={
                    "password": conf.sentinel_password,
                    "username": CharmUsers.SENTINEL_CHARM_ADMIN.value,
                },
            )
            return s.master_for("primary")

        current_val = starting_number
        config = data_queue.get(block=True)
        client = _make_client(config)

        proc_logger.info(f"Starting continuous writes from {current_val}")

        try:
            while not event.is_set():
                # Check for config updates (e.g. cluster scaling)
                if not data_queue.empty():
                    config = data_queue.get(block=False)
                    client = _make_client(config)

                try:
                    # note LPUSH returns the length of the list after the push
                    if client.lpush(ContinuousWrites.KEY, current_val):
                        if log_written_values:
                            proc_logger.info(f"Wrote value: {current_val}")
                        current_val += 1
                        # Throttle to avoid flooding small test runners
                        time.sleep(1)
                    else:
                        raise WriteFailedError("LPUSH returned 0/None")
                except Exception as e:
                    proc_logger.warning(f"Write failed at {current_val}: {e}")
                    time.sleep(2)
                    continue
        finally:
            # Persistent where we stopped
            with open(ContinuousWrites.LAST_WRITTEN_VAL_PATH, "w") as f:
                f.write(str(current_val - 1))
                os.fsync(f)


if __name__ == "__main__":
    # Example usage
    juju_env = jubilant.Juju(model="testing")
    cw = ContinuousWrites(juju=juju_env, app="valkey", initial_count=100, log_written_values=False)
    cw.clear()
    cw.start()
    time.sleep(10)
    print(f"Stats: {cw.clear()}")
