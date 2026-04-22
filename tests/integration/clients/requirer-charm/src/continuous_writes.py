#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Continuous writes daemon for Valkey integration testing.

Spawned by the requirer charm's start-continuous-writes action. Reads
connection config from a JSON file, writes incrementing integers to a
Valkey list, and tracks the last successfully written value atomically.

Usage:
    python3 continuous_writes.py <config_path> [sleep_interval]

The config JSON must contain:
    endpoints     - comma-separated "host:port,host:port,..." string
    username      - Valkey username
    password      - Valkey password
    tls_enabled   - bool (optional, default false)
    cert_path     - path to client cert PEM (required if tls_enabled)
    key_path      - path to client key PEM (required if tls_enabled)
    ca_path       - path to CA cert PEM (required if tls_enabled)
    initial_count - int to start counter from (optional, default 0)

On write failure the same counter value is retried until it succeeds before
advancing, so no gaps are introduced in the sequence.

State is written atomically to STATE_PATH after each successful write:
    {"last_written": N, "count": N}

PID is written to PID_PATH on startup and removed on exit.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from glide import (
    AdvancedGlideClientConfiguration,
    BackoffStrategy,
    GlideClient,
    GlideClientConfiguration,
    NodeAddress,
    ServerCredentials,
    TlsAdvancedConfiguration,
)
from pydantic import BaseModel

KEY = "cw_key"
CONFIG_PATH = Path("/tmp/cw_config.json")
STATE_PATH = Path("/tmp/cw_state.json")
PID_PATH = Path("/tmp/cw_daemon.pid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


class TlsConfig(BaseModel):
    """TLS certificate paths for the Glide client."""

    cert_path: str
    key_path: str
    ca_path: str


class DaemonConfig(BaseModel):
    """Connection configuration for the continuous-writes daemon."""

    endpoints: str
    username: str
    password: str
    tls: TlsConfig | None = None
    initial_count: int = 0
    clear_existing: bool = False

    @classmethod
    def from_file(cls, path: Path) -> "DaemonConfig":
        """Load and validate config from a JSON file."""
        return cls.model_validate_json(path.read_text())

    def to_file(self, path: Path) -> None:
        """Serialise config to a JSON file."""
        path.write_text(self.model_dump_json())


def _write_state_atomic(last_written: int, count: int) -> None:
    """Write state file atomically using a temp-file + rename."""
    data = json.dumps({"last_written": last_written, "count": count})
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.rename(STATE_PATH)


async def _make_client(config: DaemonConfig) -> GlideClient:
    addresses = [
        NodeAddress(host, int(port_str))
        for endpoint in config.endpoints.split(",")
        for host, port_str in [endpoint.rsplit(":", 1)]
    ]

    tls_cert = tls_key = tls_ca = None
    if config.tls is not None:
        tls_cert = Path(config.tls.cert_path).read_bytes()
        tls_key = Path(config.tls.key_path).read_bytes()
        tls_ca = Path(config.tls.ca_path).read_bytes()

    glide_config = GlideClientConfiguration(
        addresses=addresses,
        credentials=ServerCredentials(
            username=config.username,
            password=config.password,
        ),
        use_tls=config.tls is not None,
        request_timeout=1000,
        reconnect_strategy=BackoffStrategy(num_of_retries=1, factor=0, exponent_base=1),
        advanced_config=AdvancedGlideClientConfiguration(
            tls_config=TlsAdvancedConfiguration(
                client_cert_pem=tls_cert,
                client_key_pem=tls_key,
                root_pem_cacerts=tls_ca,
                use_insecure_tls=True if config.tls is not None else None,
            )
        ),
    )
    return await GlideClient.create(glide_config)


async def clear(client: GlideClient) -> None:
    """Delete the continuous-writes list key from Valkey."""
    await client.delete([KEY])
    logger.info("Cleared existing values for key '%s'.", KEY)


async def _initial_count(config: DaemonConfig, client: GlideClient) -> tuple[int, int]:
    """Return (counter, list_len) to start from, resuming from state file if present."""
    if config.clear_existing:
        try:
            await clear(client)
        except Exception as exc:
            logger.warning("Failed to clear existing values: %s", exc)
        return config.initial_count, 0

    counter = config.initial_count
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
            counter = state.get("last_written", counter) + 1
        except (json.JSONDecodeError, KeyError):
            pass

    count = 0
    try:
        count = await client.llen(KEY)
    except Exception:
        pass

    return counter, count


def _try_reload(old: DaemonConfig) -> DaemonConfig:
    """Re-read config from disk; log changes and return updated config or original on failure."""
    try:
        new = DaemonConfig.from_file(CONFIG_PATH)
    except Exception as exc:
        logger.warning("Failed to reload config: %s", exc)
        return old

    changes = []
    if old.endpoints != new.endpoints:
        changes.append(f"endpoints: {old.endpoints!r} -> {new.endpoints!r}")
    if old.username != new.username:
        changes.append(f"username: {old.username!r} -> {new.username!r}")
    if (old.tls is not None) != (new.tls is not None):
        changes.append(f"tls_enabled: {old.tls is not None} -> {new.tls is not None}")

    if changes:
        logger.info("Config reloaded — changes: %s", "; ".join(changes))
    else:
        logger.info("Config reloaded — no changes detected.")

    return new


async def _close_client(client: GlideClient | None) -> None:
    """Close client if not None, swallowing errors."""
    if client is not None:
        try:
            await client.close()
        except Exception:
            pass


async def clear_key(config: DaemonConfig) -> None:
    """Connect to Valkey and delete the continuous-writes list key."""
    client = await _make_client(config)
    try:
        await clear(client)
    finally:
        await _close_client(client)


async def _write_one(client: GlideClient, counter: int) -> tuple[int, int]:
    """Write one value, return (last_written, new_count)."""
    new_len = await client.lpush(KEY, [str(counter)])
    if not new_len:
        raise RuntimeError("LPUSH returned 0/None")
    return counter, new_len


async def run(config: DaemonConfig, sleep_interval: float) -> None:
    """Run the main write loop until SIGTERM/SIGINT."""
    stop = asyncio.Event()
    reload = asyncio.Event()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGUSR1, reload.set)

    client: GlideClient = await _make_client(config)
    counter, count = await _initial_count(config, client)
    last_written = counter - 1
    logger.info(
        "Starting continuous writes from counter=%d (existing list len=%d)", counter, count
    )

    try:
        while not stop.is_set():
            try:
                if reload.is_set():
                    reload.clear()
                    config = _try_reload(config)
                    await _close_client(client)
                    client = None
                if client is None:
                    client = await _make_client(config)
                last_written, count = await _write_one(client, counter)
                _write_state_atomic(last_written, count)
                logger.info("Wrote %d (list len=%d)", counter, count)
                counter += 1
            except Exception as exc:
                logger.warning("Write failed for counter=%d, will retry: %s", counter, exc)
                # In standalone mode, Glide locks onto the primary node during initialization and does not auto-refresh.
                # If the primary fails, the client will time out indefinitely until manually recreated, making long-term client reuse highly unreliable.
                try:
                    await _close_client(client)
                except Exception:
                    pass
                client = None

            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await _close_client(client)

    # Flush final state before exiting
    _write_state_atomic(last_written, count)
    logger.info("Daemon exiting — last_written=%d, count=%d", last_written, count)


if __name__ == "__main__":
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else CONFIG_PATH
    sleep_interval = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    config = DaemonConfig.from_file(config_path)

    PID_PATH.write_text(str(os.getpid()))
    try:
        asyncio.run(run(config, sleep_interval))
    finally:
        PID_PATH.unlink(missing_ok=True)
