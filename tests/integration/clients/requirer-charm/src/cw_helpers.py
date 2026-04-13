# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for the continuous-writes daemon used by the requirer charm."""

import enum
import logging
import os
import signal
import time
from pathlib import Path

from continuous_writes import KEY as CW_KEY
from continuous_writes import DaemonConfig, glide_client as cw_client

logger = logging.getLogger(__name__)


class CWPath(enum.Enum):
    """Paths used by the continuous-writes daemon."""

    CONFIG = Path("/tmp/cw_config.json")
    STATE = Path("/tmp/cw_state.json")
    PID = Path("/tmp/cw_daemon.pid")
    LOG = Path("/tmp/cw_daemon.log")
    CERT = Path("/tmp/cw_client.pem")
    KEY = Path("/tmp/cw_client.key")
    CA = Path("/tmp/cw_client_ca.pem")


def wait_for_pid_exit(
    pid: int, poll_interval: int = 1, max_attempts: int = 10, force_kill: bool = True
) -> bool:
    """Wait for a process to exit.

    Returns True if the process exited cleanly within max_attempts, False otherwise.
    If force_kill is True and the process is still running after max_attempts, sends SIGKILL.
    """
    for attempt in range(max_attempts):
        time.sleep(poll_interval)
        try:
            os.kill(pid, 0)  # signal 0 checks existence without sending a signal
        except ProcessLookupError:
            logger.info("Daemon PID %d exited after %d second(s).", pid, attempt * poll_interval)
            return True
        except OSError:
            pass  # EPERM — process exists but unowned; treat as still running

    logger.warning(
        "Daemon PID %d did not exit after %d second(s).",
        pid,
        max_attempts * poll_interval,
    )
    if force_kill:
        logger.warning("Sending SIGKILL to daemon PID %d.", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return False


async def cw_llen(config: DaemonConfig) -> int:
    """Return the current length of the continuous-writes list in Valkey."""
    async with cw_client(config) as client:
        return await client.llen(CW_KEY)
