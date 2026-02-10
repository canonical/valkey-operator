#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import pathlib
import signal
import sys
import time

import valkey
from tenacity import RetryError, Retrying, stop_after_attempt, wait_fixed

SENTINEL_PORT = 26379

logger = logging.getLogger(__name__)

WRITES_LAST_WRITTEN_VAL_PATH = "last_written_value"
LOG_FILE_PATH = "log_file"
continue_running = True


def continuous_writes(
    endpoints: str,
    valkey_user: str,
    valkey_password: str,
    sentinel_user: str,
    sentinel_password: str,
) -> None:
    key = "cw_key"
    count = 0

    client = valkey.Sentinel(
        [(host, SENTINEL_PORT) for host in endpoints.split(",")],
        username=valkey_user,
        password=valkey_password,
        sentinel_kwargs={"password": sentinel_password, "username": sentinel_user},
    )
    master = client.master_for("primary")

    # clean up from previous runs
    pathlib.Path(WRITES_LAST_WRITTEN_VAL_PATH).unlink(missing_ok=True)
    try:
        master.delete(key)
    except Exception:
        pass

    while continue_running:
        count += 1

        try:
            for attempt in Retrying(stop=stop_after_attempt(2), wait=wait_fixed(1)):
                with attempt:
                    result = master.set(key, str(count))
                    if not result:
                        raise ValueError
                    with open(LOG_FILE_PATH, "a") as log_file:
                        log_file.write(f"{count}\n")
        except RetryError:
            pass

        time.sleep(1)
    else:
        # write last expected written value on disk when terminating
        pathlib.Path(WRITES_LAST_WRITTEN_VAL_PATH).write_text(str(count))


def handle_stop_signal(signum, frame) -> None:
    global continue_running
    continue_running = False


def main():
    endpoints = sys.argv[1]
    valkey_user = sys.argv[2]
    valkey_password = sys.argv[3]
    sentinel_user = sys.argv[4]
    sentinel_password = sys.argv[5]

    # handle the stop signal for a graceful stop of the writes process
    signal.signal(signal.SIGTERM, handle_stop_signal)

    continuous_writes(endpoints, valkey_user, valkey_password, sentinel_user, sentinel_password)


if __name__ == "__main__":
    main()
