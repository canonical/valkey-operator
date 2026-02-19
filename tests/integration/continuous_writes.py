#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging
import multiprocessing
import queue
from contextlib import asynccontextmanager
from multiprocessing import log_to_stderr
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import jubilant
from glide import GlideClient, GlideClientConfiguration, NodeAddress, ServerCredentials
from tenacity import (
    retry,
    stop_after_attempt,
    wait_fixed,
    wait_random,
)

from literals import CharmUsers
from tests.integration.helpers import get_cluster_hostnames, get_password

logger = logging.getLogger(__name__)


class WriteFailedError(Exception):
    """Raised when a single write operation has failed."""


class ContinuousWrites:
    """Utility class for managing continuous async writes to Valkey using GLIDE."""

    KEY = "cw_key"
    LAST_WRITTEN_VAL_PATH = "last_written_value"
    VALKEY_PORT = 6379

    def __init__(
        self, juju: jubilant.Juju, app: str, initial_count: int = 0, in_between_sleep: float = 1.0
    ):
        self._juju = juju
        self._app = app
        self._is_stopped = True
        self._event = None
        self._queue = None
        self._process = None
        self._initial_count = initial_count
        self._in_between_sleep = in_between_sleep
        self._mp_ctx = multiprocessing.get_context("spawn")

    def _get_config(self) -> SimpleNamespace:
        """Fetch current cluster configuration from Juju."""
        return SimpleNamespace(
            endpoints=",".join(get_cluster_hostnames(self._juju, app_name=self._app)),
            valkey_password=get_password(self._juju, user=CharmUsers.VALKEY_ADMIN),
        )

    async def _create_glide_client(self, config: Optional[SimpleNamespace] = None) -> GlideClient:
        """Asynchronously create and return a configured GlideClient."""
        conf = config or self._get_config()
        addresses = [NodeAddress(host, self.VALKEY_PORT) for host in conf.endpoints.split(",")]

        credentials = ServerCredentials(
            username=CharmUsers.VALKEY_ADMIN.value, password=conf.valkey_password
        )

        glide_config = GlideClientConfiguration(
            addresses=addresses,
            client_name="continuous_writes_client",
            request_timeout=5000,
            credentials=credentials,
        )

        return await GlideClient.create(glide_config)

    @retry(wait=wait_fixed(5) + wait_random(0, 5), stop=stop_after_attempt(5))
    def start(self) -> None:
        """Run continuous writes in the background."""
        if not self._is_stopped:
            self.clear()

        self._is_stopped = False
        # Create primitives using the spawn context
        self._event = self._mp_ctx.Event()
        self._queue = self._mp_ctx.Queue()

        last_written_file = Path(self.LAST_WRITTEN_VAL_PATH)
        if not last_written_file.exists():
            last_written_file.write_text(str(self._initial_count))

        self._process = self._mp_ctx.Process(
            target=self._run_process,
            name="continuous_writes",
            args=(self._event, self._queue, self._initial_count, self._in_between_sleep),
        )

        self.update()
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

        asyncio.run(self._async_delete())

        last_written_file = Path(self.LAST_WRITTEN_VAL_PATH)
        if last_written_file.exists():
            last_written_file.unlink()
        return result

    @retry(wait=wait_fixed(5) + wait_random(0, 5), stop=stop_after_attempt(5))
    async def async_clear(self) -> SimpleNamespace | None:
        """Stop writes and delete the tracking key/file."""
        result = None
        if not self._is_stopped:
            result = await self.async_stop()

        await self._async_delete()

        last_written_file = Path(self.LAST_WRITTEN_VAL_PATH)
        if last_written_file.exists():
            last_written_file.unlink()
        return result

    async def _async_delete(self) -> None:
        client = await self._create_glide_client()
        try:
            await client.delete([self.KEY])
        finally:
            await client.close()

    def count(self) -> int:
        """Return number of items in the list."""
        return asyncio.run(self._async_count())

    async def _async_count(self) -> int:
        client = await self._create_glide_client()
        try:
            return await client.llen(self.KEY)
        finally:
            await client.close()

    def max_stored_id(self) -> int:
        """Return the most recently inserted ID (top of list)."""
        return asyncio.run(self._async_max_stored_id())

    async def _async_max_stored_id(self) -> int:
        client = await self._create_glide_client()
        try:
            val = await client.lindex(self.KEY, 0)
            return int(val.decode()) if val else 0
        finally:
            await client.close()

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
        result.last_expected_id = int(Path(self.LAST_WRITTEN_VAL_PATH).read_text().strip())

        return result

    @retry(wait=wait_fixed(5) + wait_random(0, 5), stop=stop_after_attempt(5))
    async def async_stop(self) -> SimpleNamespace:
        """Stop the background process and return summary statistics."""
        if not self._is_stopped and self._process:
            self._event.set()
            self._process.join(timeout=30)
            self._process.terminate()
            self._is_stopped = True

        result = SimpleNamespace()
        result.max_stored_id = await self._async_max_stored_id()
        result.count = await self._async_count()
        result.last_expected_id = int(Path(self.LAST_WRITTEN_VAL_PATH).read_text().strip())

        return result

    @staticmethod
    def _run_process(event, data_queue, starting_number: int, in_between_sleep: float):
        """Start synchronously the asyncio event loop."""
        proc_logger = log_to_stderr()
        proc_logger.setLevel(logging.INFO)

        # FIX 2: Do the blocking read synchronously BEFORE starting the async loop
        initial_config = data_queue.get(block=True)

        asyncio.run(
            ContinuousWrites._async_run(
                event, data_queue, starting_number, initial_config, in_between_sleep, proc_logger
            )
        )

    @staticmethod
    async def _async_run(
        event,
        data_queue,
        starting_number: int,
        initial_config: SimpleNamespace,
        in_between_sleep: float,
        proc_logger: logging.Logger,
    ):
        """Async loop for writing data continuously."""

        async def _make_client(conf: SimpleNamespace) -> GlideClient:
            addresses = [
                NodeAddress(host, ContinuousWrites.VALKEY_PORT)
                for host in conf.endpoints.split(",")
            ]
            credentials = ServerCredentials(
                username=CharmUsers.VALKEY_ADMIN.value,
                password=conf.valkey_password,
            )
            glide_config = GlideClientConfiguration(
                addresses=addresses,
                client_name="continuous_writes_worker",
                request_timeout=5000,
                credentials=credentials,
            )
            return await GlideClient.create(glide_config)

        @asynccontextmanager
        async def with_client(conf: SimpleNamespace):
            client = await _make_client(conf)
            try:
                yield client
            finally:
                await client.close()

        current_val = starting_number
        config = initial_config
        # client = await _make_client(config)

        proc_logger.info(f"Starting continuous async writes from {current_val}")

        try:
            while not event.is_set():
                try:
                    config = data_queue.get_nowait()
                    # await client.close()
                    # client = await _make_client(config)
                    proc_logger.info("Configuration updated, client reconnected.")
                except queue.Empty:
                    pass

                try:
                    proc_logger.info(f"Writing value: {current_val}")
                    async with with_client(config) as client:
                        if not (
                            res := await asyncio.wait_for(
                                client.lpush(ContinuousWrites.KEY, [str(current_val)]), timeout=5
                            )
                        ):
                            raise WriteFailedError("LPUSH returned 0/None")
                    proc_logger.info(f"Length after write: {res}")
                    await asyncio.sleep(in_between_sleep)
                except Exception as e:
                    proc_logger.warning(f"Write failed at {current_val}: {e}")
                finally:
                    if event.is_set():
                        break

                current_val += 1

        finally:
            Path(ContinuousWrites.LAST_WRITTEN_VAL_PATH).write_text(str(current_val))
            proc_logger.info("Continuous writes process exiting.")


if __name__ == "__main__":
    import jubilant

    juju_env = jubilant.Juju(model="testing")
    cw = ContinuousWrites(juju=juju_env, app="valkey", in_between_sleep=0.5)
    cw.clear()
    cw.start()
    print("Continuous writes started. Press Enter to stop...")
    input()
    stats = cw.clear()
    print(f"Stopped. Stats: {stats}")
