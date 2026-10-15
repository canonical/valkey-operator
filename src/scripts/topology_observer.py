# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Topology observer class for checking changes in Primary/Replica topology."""

import logging
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

from literals import (
    PRIMARY_NAME,
    TOPOLOGY_OBSERVER_LOG_BACKUPS,
    TOPOLOGY_OBSERVER_LOG_FILENAME,
    TOPOLOGY_OBSERVER_LOG_MAX_BYTES,
    TOPOLOGY_OBSERVER_TLS_CA_FILENAME,
)

# use global variable for gracefully handling stop signals
continue_running = True


def dispatch(unit_name: str, charm_dir: str) -> None:
    """Dispatch a Juju custom event."""
    custom_event = "topology_changed"

    juju_run_command = "/usr/bin/juju-exec"
    dispatch_command = f"JUJU_DISPATCH_PATH=hooks/{custom_event} {charm_dir}/dispatch"

    subprocess.run(
        [juju_run_command, "-u", unit_name, dispatch_command],
        check=True,
        capture_output=True,
        text=True,
    )


def handle_stop_signal(signum, frame) -> None:
    """Stop the execution gracefully."""
    global continue_running
    continue_running = False


def main() -> None:
    """Start a Sentinel client and check changes to primary."""
    hosts, username, password, tls, unit_name, charm_dir = sys.argv[1:]
    logging.basicConfig(
        handlers=[
            RotatingFileHandler(
                f"{charm_dir}/{TOPOLOGY_OBSERVER_LOG_FILENAME}",
                maxBytes=TOPOLOGY_OBSERVER_LOG_MAX_BYTES,
                backupCount=TOPOLOGY_OBSERVER_LOG_BACKUPS,
            )
        ],
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=logging.DEBUG,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        # imported after logging is set up so a broken venv gets logged
        from valkey.sentinel import MasterNotFoundError, Sentinel

        # handle the stop signal for a graceful stop of the subscription client
        signal.signal(signal.SIGTERM, handle_stop_signal)

        logging.info("Starting new observer for hosts %s with tls=%s", hosts, tls)

        host_list = hosts.split(",")
        addresses = [
            (hostname, int(port)) for host in host_list for hostname, port in [host.split(":")]
        ]
        tls_enabled = True if tls == "True" else False
        sentinel_kwargs = {
            "username": username,
            "password": password,
            "decode_responses": True,
            "ssl": tls_enabled,
            "ssl_ca_certs": f"{charm_dir}/{TOPOLOGY_OBSERVER_TLS_CA_FILENAME}"
            if tls_enabled
            else None,
        }

        # only updated once the charm has been told, so a failed dispatch is retried
        notified_primary = ""

        while continue_running:
            time.sleep(1)

            sentinel = Sentinel(
                sentinels=addresses,
                socket_timeout=0.1,
                sentinel_kwargs=sentinel_kwargs,
            )

            try:
                primary_name = sentinel.discover_master(PRIMARY_NAME)[0]
            except MasterNotFoundError as e:
                logging.error("Failed to discover primary: %s", e)
                continue

            if notified_primary == "":
                notified_primary = primary_name
                continue

            if primary_name == notified_primary:
                continue

            logging.info(
                "Primary change detected: previously %s, now %s", notified_primary, primary_name
            )
            try:
                dispatch(unit_name, charm_dir)
            except subprocess.CalledProcessError as e:
                logging.error("Error when dispatching Juju event: %s: %s", e, e.stderr)
                continue

            notified_primary = primary_name

        else:
            logging.info("Gracefully stopping observer")
    except Exception:
        # stderr goes to /dev/null, so log the traceback to the file
        logging.exception("Topology observer crashed")
        raise


if __name__ == "__main__":
    main()
