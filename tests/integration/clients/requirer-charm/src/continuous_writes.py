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
from dataclasses import dataclass
from pathlib import Path

from glide import (
    AdvancedGlideClientConfiguration,
    GlideClient,
    GlideClientConfiguration,
    NodeAddress,
    ServerCredentials,
    TlsAdvancedConfiguration,
)

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


@dataclass
class TlsConfig:
    """TLS certificate paths for the Glide client."""

    cert_path: str
    key_path: str
    ca_path: str

    def to_dict(self) -> dict[str, str]:
        """Serialise TLS config to a dict."""
        return {
            "cert_path": self.cert_path,
            "key_path": self.key_path,
            "ca_path": self.ca_path,
        }


@dataclass
class DaemonConfig:
    """Connection configuration for the continuous-writes daemon."""

    endpoints: str
    username: str
    password: str
    tls: TlsConfig | None = None
    initial_count: int = 0

    @classmethod
    def from_file(cls, path: Path) -> "DaemonConfig":
        """Load and validate config from a JSON file."""
        data = json.loads(path.read_text())
        tls = (
            TlsConfig(
                cert_path=data["cert_path"], key_path=data["key_path"], ca_path=data["ca_path"]
            )
            if data.get("tls_enabled")
            else None
        )
        return cls(
            endpoints=data["endpoints"],
            username=data["username"],
            password=data["password"],
            tls=tls,
            initial_count=data.get("initial_count", 0),
        )

    def to_file(self, path: Path) -> None:
        """Serialise config to a JSON file."""
        data: dict[str, object] = {
            "endpoints": self.endpoints,
            "username": self.username,
            "password": self.password,
            "tls_enabled": self.tls is not None,
            "initial_count": self.initial_count,
        }
        if self.tls is not None:
            data.update(self.tls.to_dict())
        path.write_text(json.dumps(data))


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
        request_timeout=2000,
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


async def run(config: DaemonConfig, sleep_interval: float) -> None:
    """Run the main write loop until SIGTERM/SIGINT."""
    stop = asyncio.Event()

    def _handle_stop(*_):
        stop.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _handle_stop)
    loop.add_signal_handler(signal.SIGINT, _handle_stop)

    # Resume from previous state if present
    counter = config.initial_count
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
            counter = state.get("last_written", counter) + 1
        except (json.JSONDecodeError, KeyError):
            pass

    last_written = counter - 1
    # LLEN at startup to pick up existing count in case of restart
    count = 0
    try:
        client = await _make_client(config)
        try:
            count = await client.llen(KEY)
        finally:
            await client.close()
    except Exception:
        pass

    logger.info(
        "Starting continuous writes from counter=%d (existing list len=%d)", counter, count
    )

    while not stop.is_set():
        try:
            client = await _make_client(config)
            try:
                new_len = await asyncio.wait_for(client.lpush(KEY, [str(counter)]), timeout=5)
            finally:
                await client.close()

            if not new_len:
                raise RuntimeError("LPUSH returned 0/None")

            last_written = counter
            count = new_len
            _write_state_atomic(last_written, count)
            logger.info("Wrote %d (list len=%d)", counter, count)
        except Exception as exc:
            # Write failed — log and skip without updating last_written.
            # counter still increments so a gap is introduced in the sequence,
            # making failed writes detectable during consistency checks.
            logger.warning("Write failed for counter=%d: %s", counter, exc)

        counter += 1

        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_interval)
        except asyncio.TimeoutError:
            pass

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
