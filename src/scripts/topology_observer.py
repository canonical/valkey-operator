# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Topology observer class for checking changes in Primary/Replica topology."""

import logging
import signal
import subprocess
import sys
import time

from valkey.sentinel import MasterNotFoundError, Sentinel

from literals import PRIMARY_NAME, TOPOLOGY_OBSERVER_TLS_CA_FILE

# use global variable for gracefully handling stop signals
continue_running = True

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.DEBUG,
    datefmt="%Y-%m-%d %H:%M:%S",
)


def dispatch(unit_name: str, charm_dir: str) -> None:
    """Dispatch a Juju custom event."""
    custom_event = "topology_changed"

    juju_run_command = "/usr/bin/juju-exec"
    dispatch_command = f"JUJU_DISPATCH_PATH=hooks/{custom_event} {charm_dir}/dispatch"

    subprocess.run([juju_run_command, "-u", unit_name, dispatch_command])


def handle_stop_signal(signum, frame) -> None:
    """Stop the execution gracefully."""
    global continue_running
    continue_running = False


def main() -> None:
    """Start a Sentinel client and check changes to primary."""
    hosts, username, password, tls, unit_name, charm_dir = sys.argv[1:]

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
        "ssl_ca_certs": TOPOLOGY_OBSERVER_TLS_CA_FILE if tls_enabled else None,
    }

    primary_name = ""
    previous_primary = ""

    while continue_running:
        time.sleep(1)

        if primary_name != "":
            previous_primary = primary_name

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

        if previous_primary == "" or primary_name == previous_primary:
            continue

        logging.info(
            "Primary change detected: previously %s, now %s", previous_primary, primary_name
        )
        dispatch(unit_name, charm_dir)

    else:
        logging.info("Gracefully stopping observer")


if __name__ == "__main__":
    main()
